"""
Contains the base class for communicating with a 32-bit library from 64-bit Python.

The :class:`~.server32.Server32` class is used in combination with the
:class:`~.client64.Client64` class to communicate with a 32-bit shared library
from 64-bit Python.
"""
import os
import sys
import site
import uuid
import time
import random
import tempfile
import subprocess

try:
    import cPickle as pickle
except ImportError:
    import pickle

from msl.loadlib import IS_PYTHON2, IS_PYTHON3, SERVER_FILENAME

if IS_PYTHON2:
    from httplib import HTTPConnection
    from httplib import HTTPException
elif IS_PYTHON3:
    from http.client import HTTPConnection
    from http.client import HTTPException
else:
    raise NotImplementedError('Python major version is not 2 or 3')


class Client64(HTTPConnection):
    """Base class for communicating with a 32-bit library from 64-bit Python.

    Starts a 32-bit server, :class:`~.server32.Server32`, to host a Python module
    that is a wrapper around a 32-bit library. The *client* module runs within
    a 64-bit Python interpreter and it sends a request to the server which calls
    the 32-bit library to execute the request. The server then provides a
    response back to the client.

    Parameters
    ----------
    module32 : :obj:`str`
        The name of the Python module that is to be imported by the 32-bit server.
    host : :obj:`str`, optional
        The IP address of the 32-bit server. Default is '127.0.0.1'.
    port : :obj:`int`, optional
        The port to open on the 32-bit server. Default is :obj:`None` *(which means 
        to automatically find a port that is available).*
    timeout : :obj:`float`, optional
        The maximum number of seconds to wait to establish a connection to the 
        32-bit server. Default is 10.
    quiet : :obj:`bool`, optional
        Whether to hide :obj:`sys.stdout` messages from the 32-bit server. 
        Default is :obj:`True`.
    append_sys_path : :obj:`str` or :obj:`list` of :obj:`str`, optional
        Append path(s) to the 32-bit server's :obj:`sys.path` variable. 
        Default is :obj:`None`.
    append_environ_path : :obj:`str` or :obj:`list` of :obj:`str`, optional
        Append path(s) to the 32-bit server's :obj:`os.environ['PATH'] <os.environ>` 
        variable. This can be useful if the library that is being loaded requires 
        additional libraries that must be available on ``PATH``. Default is :obj:`None`.
    **kwargs
        Keyword arguments that will be passed to the :class:`~.server32.Server32`
        subclass. The data type of each value is not preserved. It will be a string
        at the constructor of the :class:`~.server32.Server32` subclass.

    Note
    ----
    If `module32` is not located in the current working directory then you 
    must either specify the full path to `module32` **or** you can
    specify the folder where `module32` is located by passing a value to the
    `append_sys_path` parameter. Using the `append_sys_path` option also allows
    for any other modules that `module32` may depend on to also be included
    in :obj:`sys.path` so that those modules can be imported when `module32`
    is imported.

    Raises
    ------
    FileNotFoundError
        If the frozen executable cannot be found. 
    TypeError
        If the data type of `append_sys_path` or `append_environ_path` is invalid.
    :class:`~http.client.HTTPException`
        If the connection to the 32-bit server cannot be established.
    """
    def __init__(self, module32, host='127.0.0.1', port=None, timeout=10.0, quiet=True,
                 append_sys_path=None, append_environ_path=None, **kwargs):

        self._is_active = False

        if port is None:
            while True:
                port = random.randint(1024, 65535)
                if not self.port_in_use(port):
                    break

        # the temporary file to use to save the pickle'd data
        self._pickle_temp_file = os.path.join(tempfile.gettempdir(), str(uuid.uuid4()))

        # select the highest-level pickle protocol to use based on the version of python
        major, minor = sys.version_info.major, sys.version_info.minor
        if (major <= 1) or (major == 2 and minor < 3):
            self._pickle_protocol = 1
        elif major == 2:
            self._pickle_protocol = 2
        elif (major == 3) and (minor < 4):
            self._pickle_protocol = 3
        else:
            self._pickle_protocol = pickle.HIGHEST_PROTOCOL

        # make sure that the server32 executable exists
        server_exe = os.path.join(os.path.dirname(__file__), SERVER_FILENAME)
        if not os.path.isfile(server_exe):
            raise FileNotFoundError('Cannot find ' + server_exe)

        cmd = [
            server_exe,
            '--module', module32,
            '--host', host,
            '--port', str(port)
        ]

        # include paths to the 32-bit server's sys.path
        _append_sys_path = site.getsitepackages()
        if append_sys_path is not None:
            if isinstance(append_sys_path, str):
                _append_sys_path.append(append_sys_path.strip())
            elif isinstance(append_sys_path, (list, tuple)):
                _append_sys_path.extend(append_sys_path)
            else:
                raise TypeError('append_sys_path must be a str, list or tuple')
        cmd.extend(['--append-sys-path', ';'.join(_append_sys_path).strip()])

        # include paths to the 32-bit server's os.environ['PATH']
        if append_environ_path is not None:
            if isinstance(append_environ_path, str):
                env_str = append_environ_path.strip()
            elif isinstance(append_environ_path, (list, tuple)):
                env_str = ';'.join(append_environ_path).strip()
            else:
                raise TypeError('append_environ_path must be a str, list or tuple')
            if env_str:
                cmd.extend(['--append-environ-path', env_str])

        # include any keyword arguments
        if kwargs:
            kw_str = ';'.join('{}={}'.format(key, value) for key, value in kwargs.items())
            cmd.extend(['--kwargs', kw_str])

        if quiet:
            cmd.append('--quiet')

        # start the server, cannot use subprocess.call() because it blocks
        subprocess.Popen(cmd, stderr=sys.stderr, stdout=sys.stderr)

        # wait for the server to be running -- essentially this is the subprocess.wait() method
        stop = time.time() + max(0.0, timeout)
        while True:
            if self.port_in_use(port):
                break
            if time.time() > stop:
                m = 'Timeout after {:.1f} s. Could not connect to {}:{}'.format(timeout, host, port)
                raise HTTPException(m)

        # start the connection
        HTTPConnection.__init__(self, host, port)
        self._is_active = True

        # Problem: If one creates a Client64 object and does not send a request to the
        #   server then when the __del__ function gets called to shutdown_server32() a
        #   "LookupError: unknown encoding: idna" error is thrown.
        #   Trying "import encodings.idna" at the top of this module did not fix it.
        # Solution: It seems as though all we have to do is send one request to the server
        #   before shutdown_server32() is called. So, just request the lib path.
        self._lib32_path = self.request32('LIB32_PATH')

    def __repr__(self):
        msg = '{} object at {}'.format(self.__class__.__name__, hex(id(self)))
        if self._is_active:
            lib = os.path.basename(self._lib32_path)
            return msg + ' hosting {} on http://{}:{}'.format(lib, self.host, self.port)
        else:
            return msg + ' has stopped the server'

    @property
    def lib32_path(self):
        """The path to the 32-bit library.
        
        Returns
        -------
        :obj:`str`
            The path to the 32-bit shared-library file.
        """
        return self._lib32_path

    def request32(self, method32, *args, **kwargs):
        """Send a request to the 32-bit server.

        Parameters
        ----------
        method32 : :obj:`str`
            The name of the method to call in the :class:`~.server32.Server32` subclass.
        *args
            The arguments that the `method32` method in the :class:`~.server32.Server32` 
            subclass requires.
        **kwargs
            The keyword arguments that the `method32` method in the 
            :class:`~.server32.Server32` subclass requires.

        Returns
        -------
        The response from the 32-bit server.

        Raises
        ------
        :class:`~http.client.HTTPException`
            If there was an error processing the request on the 32-bit server.
        """
        if not self._is_active:
            raise HTTPException('The 32-bit server is not active')

        if method32 == 'SHUTDOWN_SERVER32':
            self.request('GET', '/SHUTDOWN_SERVER32')
            return

        request = '/{}:{}:{}'.format(method32, self._pickle_protocol, self._pickle_temp_file)
        with open(self._pickle_temp_file, 'wb') as f:
            pickle.dump(args, f, protocol=self._pickle_protocol)
            pickle.dump(kwargs, f, protocol=self._pickle_protocol)
        self.request('GET', request)

        response = self.getresponse()
        if response.status == 200:  # everything is OK
            with open(self._pickle_temp_file, 'rb') as f:
                result = pickle.load(f)
            return result
        raise HTTPException(response.read().decode())

    def shutdown_server32(self):
        """Shutdown the 32-bit server.
        
        This method stops the process that is running the 32-bit server executable
        and it deletes the temporary file that is used to save the serialized 
        :mod:`pickle`\'d data which is passed between the 32-bit server and the
        64-bit client.

        Note
        ----
        This method gets called automatically when the :class:`~.client64.Client64`
        object gets destroyed.
        """
        if self._is_active:
            self.request32('SHUTDOWN_SERVER32')
            if os.path.isfile(self._pickle_temp_file):
                os.remove(self._pickle_temp_file)
            self.close()
            self._is_active = False

    def __del__(self):
        self.shutdown_server32()

    @staticmethod
    def port_in_use(port):
        """Uses netstat_ to determine if the network port is in use.
        
        .. _netstat: http://www.computerhope.com/unix/unetstat.htm

        Parameters
        ----------
        port : :obj:`int`
            The port number to test.

        Returns
        -------
        :obj:`bool`
            Whether the port is in use.        
        """
        p = subprocess.Popen(['netstat', '-an'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.communicate()[0].decode().find(':{} '.format(port)) > 0
