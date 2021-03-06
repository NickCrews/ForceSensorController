import atexit
import glob
import multiprocessing
import sys
import time

import bluetooth as bt
import numpy.random as rand
import serial


class SerialScaleSearcher(object):
    """Abstract class used to searching for scales connected via USB serial cable.

    This search is pretty fast so we don't have to do it in a different process"""

    availableScales = []

    def __init__(self):
        raise NotImplementedError(
            "Cannot instantiate the helper class SerialScaleSearcher"
        )

    @classmethod
    def update(cls):
        # remove dead scales
        cls.availableScales = [s for s in cls.availableScales if s.isOpen()]

        # get all possible ports
        if sys.platform.startswith("win"):
            ports = ["COM%s" % (i + 1) for i in range(256)]
        elif sys.platform.startswith("linux") or sys.platform.startswith("cygwin"):
            # this excludes your current terminal "/dev/tty"
            ports = glob.glob("/dev/tty[A-Za-z]*")
        elif sys.platform.startswith("darwin"):
            ports = glob.glob("/dev/tty.*")
        else:
            raise EnvironmentError("Unsupported platform")

        # filter out the bad ones, ignoring ones we already have added and add them
        alreadyOpenPorts = [
            s.port for s in cls.availableScales if isinstance(s, SerialScale)
        ]
        for port in ports:
            if port in alreadyOpenPorts:
                continue
            try:
                # try to open it
                s = serial.Serial(port)
                s.close()
                # hurray, we got here, so this is a good port. Add it.
                cls.availableScales.append(SerialScale(port))
            except (OSError, serial.SerialException):
                pass


class BluetoothScaleSearcher(object):
    """Abstract class used to search for available bluetooth scales.

    The actual search is blocking, and takes a few seconds, so that is done in a different process"""

    availableScales = []
    _amSearchingFlag = multiprocessing.Event()
    Q = multiprocessing.Queue()

    SCALE_NAME = "HC-05"

    def __init__(self):
        raise NotImplementedError(
            "Cannot instantiate the helper class SerialScaleSearcher"
        )

    @classmethod
    def update(cls):
        # prune dead scales
        cls.availableScales = [s for s in cls.availableScales if s.isOpen()]
        # add create new Scales
        while not cls.Q.empty():
            addr, name = cls.Q.get()
            scale = BluetoothScale(addr, name)
            cls.availableScales.append(scale)

        # maybe skip the rest
        if cls._amSearchingFlag.is_set():
            return

        def search():
            try:
                print("starting scan for bluetooth scales")
                nearby_devices = bt.discover_devices(
                    lookup_names=True, flush_cache=True
                )
                for addr, name in nearby_devices:
                    print("found a device", addr, name)
                    if name == cls.SCALE_NAME and addr not in openAddresses:
                        cls.Q.put((addr, name))
            except bt.BluetoothError as e:
                print(e)
                pass
            finally:
                cls._amSearchingFlag.clear()

        openAddresses = [s.address for s in cls.availableScales]
        p = multiprocessing.Process(target=search)
        p.daemon = True
        cls._amSearchingFlag.set()
        p.start()


def updateAvailableScales():
    # SerialScaleSearcher.update()
    BluetoothScaleSearcher.update()
    pass


def availableScales():
    result = SerialScaleSearcher.availableScales
    result.extend(BluetoothScaleSearcher.availableScales)
    return result


class Scale(object):
    """Abstract class which is inherited by SerialScale and BluetoothScale"""

    def __init__(self):
        raise NotImplementedError("Cannot instantiate the abstract class Scale")

    def isOpen(self):
        raise NotImplementedError("isOpen() must be overriden in subclasses")

    def close(self):
        raise NotImplementedError("close() must be overriden in subclasses")

    def read(self):
        raise NotImplementedError("read() must be overriden in subclasses")


class SerialScale(Scale):
    """A scale which is connected via USB serial cable"""

    MAX_BUFFERED_READINGS = 10000

    def __init__(self, port, baudrate=9600):

        self.port = port
        self._baudrate = baudrate

        # ok, the serial is open, now create the process to constantly read from the port
        self.readingsQ = multiprocessing.Queue(self.MAX_BUFFERED_READINGS)
        self.commandQ = multiprocessing.Queue()
        self.reader = SerialReader(port, baudrate, self.readingsQ, self.commandQ)
        self.reader.start()

    def __repr__(self):
        status = "open" if self.isOpen() else "closed"
        return (
            status
            + " Serial Scale at port "
            + self.port
            + " with baudrate "
            + str(self.baudrate)
        )

    def __str__(self):
        return "Serial Scale at " + self.port

    def isOpen(self):
        return self.reader.is_alive()

    def close(self):
        # poison pill
        self.commandQ.put(None)

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, newval):
        self._baudrate = newval
        self.commandQ.put({"attr": "baudrate", "val": newval})

    def read(self):
        readings = []
        while not self.readingsQ.empty():
            readings.append(self.readingsQ.get())
        return readings


class SerialReader(multiprocessing.Process):
    """Used by SerialScale to read from the scale smoothly in a different process"""

    # in seconds
    LINK_TIMEOUT = 5
    READ_TIMEOUT = 1

    MAX_PACKET_SIZE = 20

    def __init__(self, portname, baudrate, readingsQ, commandQ):
        super(SerialReader, self).__init__()
        self.daemon = True

        self.portname = portname
        self._baudrate = baudrate
        self.readingsQ = readingsQ
        self.commandQ = commandQ

        self._ser = None
        atexit.register(self.close)

    def run(self):
        self._openPort()
        self._waitForLink()
        last = time.time()
        while self._ser.is_open:
            # print('going through loop')
            # check for updates from outside this thread
            if not self.commandQ.empty():
                cmd = self.commandQ.get()
                if cmd is None:
                    # poison pill, exit this process
                    break
                else:
                    setattr(self, cmd["attr"], cmd["val"])
            # read a line or timeout and return empty string
            line = self._readline()
            if line:
                try:
                    reading = int(line)
                except:
                    # must have had trouble parsing. probably the baudrate is wrong, but we can ignore it
                    continue

                # Throw out the oldeast reading if the Q is full
                while self.readingsQ.full():
                    self.readingsQ.get()
                pair = (time.time(), reading)
                self.readingsQ.put(pair)
            else:
                # we didn't read anything, must have timeout out
                print("didn't read anything")
                break
        self.close()

    def _readline(self):
        chars = []
        for i in range(self.MAX_PACKET_SIZE):
            try:
                c = self._ser.read(1)
            except serial.SerialException as e:
                # probably some I/O problem such as disconnected USB serial
                return None
            if not c:
                # timed out on read of individual byte
                return None
            chars.append(c)
            # check last two characters
            if chars[-2:] == [b"\r", b"\n"]:
                # looks promising...
                try:
                    strs = [c.decode() for c in chars[:-2]]
                    result = "".join(strs)
                    return result
                except:
                    # problem parsing, must be some other problem
                    return "error"
            else:
                # got end of line or there's a problem with baudrate so we never get eol
                return None

    def _openPort(self):
        # try to open the serial port
        try:
            self._ser = serial.Serial(self.portname, baudrate=self.baudrate)
        except serial.SerialException as e:
            raise e
        except ValueError as e:
            raise e
        if not self._ser.is_open:
            raise serial.SerialException(
                "couldn't open the port {port_name}".format(port_name=self._ser.name)
            )
        self._ser.timeout = self.READ_TIMEOUT
        print("successfully opened serial port", self.portname)

    def _waitForLink(self):
        """The Arduino reboots when it initiates a USB serial connection, so wait for it to resume streaming readings"""
        start_time = time.time()
        while True:
            time.sleep(0.1)
            if self._ser.in_waiting:
                # We got a reading! Hurray!
                break
            if time.time() - start_time > self.LINK_TIMEOUT:
                # Something went wrong the arduino took too long
                self._ser.close()
                break

    def close(self):
        if self._ser:
            self._ser.close()

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, newval):
        self._baudrate = newval
        if self._ser:
            self._ser.baudrate = newval


class BluetoothScale(Scale):

    MAX_BUFFERED_READINGS = 10000

    def __init__(self, address, name):
        self.address = address
        self.name = name

        self.readingsQ = multiprocessing.Queue(self.MAX_BUFFERED_READINGS)
        self.quitFlag = multiprocessing.Event()
        self.reader = BluetoothReader(self.address, self.readingsQ, self.quitFlag)
        self.reader.start()

    def __repr__(self):
        status = "open" if self.isOpen() else "closed"
        return (
            status
            + " Bluetooth Scale at address "
            + self.address
            + " with name "
            + self.name
        )

    def __str__(self):
        return "Bluetooth Scale " + self.name

    def close(self):
        self.quitFlag.set()

    def isOpen(self):
        return self.reader.is_alive()

    def read(self):
        readings = []
        while not self.readingsQ.empty():
            readings.append(self.readingsQ.get())
        return readings


class BluetoothReader(multiprocessing.Process):

    PORT = 1
    TIMEOUT = 10
    MAX_PACKET_SIZE = 10

    def __init__(self, address, readingQ, quitFlag):
        super(BluetoothReader, self).__init__()
        self.daemon = True

        self._address = address
        self._sock = None
        atexit.register(self._close)

        self.readingQ = readingQ
        self.quitFlag = quitFlag

    def run(self):
        self._sock = bt.BluetoothSocket(bt.RFCOMM)
        self._sock.connect((self._address, self.PORT))
        self._sock.settimeout(self.TIMEOUT)
        while not self.quitFlag.is_set():
            try:
                rawReading = self._readline()
                reading = int(rawReading)
                now = time.time()
                self.readingQ.put((now, reading))
            except IOError as e:
                print(e)
                break
            except ValueError as e:
                print(e)

        self._close()

    def _readline(self):
        result = ""
        for i in range(self.MAX_PACKET_SIZE):
            byte = self._sock.recv(1)
            result += byte
            if result.endswith("\r\n"):
                return result[:-2]
        raise IOError(
            "lost connection with bluetooth scale at address %s"
            % str(self._sock.getsockname())
        )

    def _close(self):
        if self._sock:
            self._sock.close()


class PhonyScale(Scale):
    """Useful for generating random noise for testing GUI if an actual scale isn't present"""

    SAMPLE_PERIOD = 1.0 / 80

    def __init__(self):
        self.last = time.time()
        self.baudrate = 9600

    def __str__(self):
        return "Phony Scale"

    def isOpen():
        return True

    def read(self):
        now = time.time()
        readings = []
        for timestamp in self.frange(self.last, now, self.SAMPLE_PERIOD):
            val = int(rand.normal() * 100)
            readings.append((timestamp, val))
        return readings

    def close(self):
        pass

    @staticmethod
    def frange(start, stop=None, inc=None):
        """A range() method for floats"""
        if stop is None:
            stop = start
            start = 0.0
        if inc is None:
            inc = 1.0
        i = 1
        result = start

        def shouldContinue(current, cutoff):
            if inc > 0:
                return current < cutoff
            else:
                return current > cutoff

        while shouldContinue(result, stop):
            yield result
            result = start + i * inc
            i += 1
