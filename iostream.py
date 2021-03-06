# vim: fileencoding=utf-8

from __future__ import absolute_import, division, with_statement

import collections
import errno
import logging
import os
import socket
import sys
import re

from tornado import ioloop
from tornado import stack_context
from tornado.util import b, bytes_type

try:
    import ssl  # Python 2.6+
except ImportError:
    ssl = None


class IOStream(object):

    """ 提供了非阻塞的一个write()方法和一系列的read_*()方法，它们都有callback。
    在ioloop的基础之上实现了异步的IO操作（异步操作，非异步IO）。
    一个IOStream对象只工作在一个socket上，根据不同的状态来完成不同的操作。 """

    def __init__(self, socket, io_loop=None, max_buffer_size=104857600, read_chunk_size=4096):
        self.socket = socket                               # 该iostream关联的socket（为None则表明已经关闭）
        self.socket.setblocking(False)                     # 非阻塞
        self.io_loop = io_loop or ioloop.IOLoop.instance() # 关联的ioloop
        self.error = None

        self.max_buffer_size = max_buffer_size             # buffer最大大小
        self.read_chunk_size = read_chunk_size             # 一次读取的大小

        self._read_buffer = collections.deque()            # 读buffer
        self._write_buffer = collections.deque()           # 写buffer（不为空则表示当前iostream正在写）
        self._read_buffer_size = 0                         # 读buffer当前大小
        self._write_buffer_frozen = False

        ## iostream有如下4种读取状态：
        self._read_delimiter = None     # 若该变量非None，则读取直到某一分隔符，同时该变量就是要求读到的分隔符
        self._read_regex = None         # 若该变量非None，则读取直到某一正则，同时该变量就是要求读到的正则
        self._read_bytes = None         # 若该变量非None，则读取固定的字符数，同时该变量就是要求读到的字符数
        self._read_until_close = False  # 若该变量为True，则读取直到关闭

        ## iostream有如下的回调：
        ## 所有这些callback在设置时都使用stack_context.wrap包装，并加入到ioloop中调用，
        ## 由于这些回调都能体现iostream当前的状态，所以一旦被调用，被调用的回调即被清空，也确保了只被调用一次
        self._read_callback = None      # 读回调（不为None则表示当前iostream正在读）
        self._streaming_callback = None # 流回调
        self._write_callback = None     # 写回调
        self._close_callback = None     # 关闭回调
        self._connect_callback = None   # 连接成功回调

        ## iostream有如下一些辅助状态：
        self._connecting = False    # 连接标志（不为False则表示当前iostream正在连接）
        self._state = None          # (IOLoop.NONE, IOLoop.READ, IOLoop.WRITE, IOLoop.ERROR)，与io_loop的注册保持一致.
        self._pending_callbacks = 0 # 未决的回调数量

    def connect(self, address, callback=None):
        """ 发起连接 """
        self._connecting = True # 收到可写通知时检查此标志
        try:
            self.socket.connect(address)
        except socket.error, e:
            if e.args[0] not in (errno.EINPROGRESS, errno.EWOULDBLOCK): # 连接失败
                logging.warning("Connect error on fd %d: %s", self.socket.fileno(), e)
                self.close() # connect调用失败则只能关闭socket(参见UNP.V1-4.3)
                return
        self._connect_callback = stack_context.wrap(callback) # 设置连接回调
        self._add_io_state(self.io_loop.WRITE) # 注册写通知到io_loop(参见`man 2 connect`, EINPROGRESS)

    def read_until_regex(self, regex, callback):
        """ 读取直到某一正则。 """
        self._set_read_callback(callback)    # 设置读回调
        self._read_regex = re.compile(regex) # 设置要读取的正则
        self._try_inline_read()

    def read_until(self, delimiter, callback):
        """ 读取直到某一分隔符。 """
        self._set_read_callback(callback)    # 设置读回调
        self._read_delimiter = delimiter     # 设置要读取的定界符
        self._try_inline_read()

    def read_bytes(self, num_bytes, callback, streaming_callback=None):
        """ 读取固定的字符数。
        如果streaming_callback不为空，则它将处理所有的数据，callback得到的参数将为空。 """
        self._set_read_callback(callback)    # 设置读回调
        assert isinstance(num_bytes, (int, long))
        self._read_bytes = num_bytes         # 设置要读取的字符数
        self._streaming_callback = stack_context.wrap(streaming_callback)
        self._try_inline_read()

    def read_until_close(self, callback, streaming_callback=None):
        """ 读取直到关闭。
        如果streaming_callback不为空，则它将处理所有的数据，callback得到的参数将为空。 """
        self._set_read_callback(callback)
        self._streaming_callback = stack_context.wrap(streaming_callback)
        if self.closed(): # 如果已经关闭则一次性消费完整个_read_buffer然后返回
            if self._streaming_callback is not None:
                self._run_callback(self._streaming_callback, self._consume(self._read_buffer_size))
            self._run_callback(self._read_callback, self._consume(self._read_buffer_size))
            self._streaming_callback = None
            self._read_callback = None
            return
        self._read_until_close = True
        self._streaming_callback = stack_context.wrap(streaming_callback) # 设置好_streaming_callback后注册io_loop
        self._add_io_state(self.io_loop.READ)

    def write(self, data, callback=None):
        """ 将给定的data写到流中。callback会在所有的写缓冲都写入到流之后被调用。"""
        assert isinstance(data, bytes_type)
        self._check_closed()
        # 不要把''放进来，会被当成无数据
        if data:
            WRITE_BUFFER_CHUNK_SIZE = 128 * 1024
            # 把超过大小的字符串打散后再追加到_write_buffer中
            if len(data) > WRITE_BUFFER_CHUNK_SIZE:
                for i in range(0, len(data), WRITE_BUFFER_CHUNK_SIZE):
                    self._write_buffer.append(data[i:i + WRITE_BUFFER_CHUNK_SIZE])
            else:
                self._write_buffer.append(data)
        self._write_callback = stack_context.wrap(callback)
        if not self._connecting: # 如果是正在连接中就先别写
            self._handle_write()
            if self._write_buffer: # 没有写完，注册个可写通知下次再写
                self._add_io_state(self.io_loop.WRITE)
            self._maybe_add_error_listener()

    def set_close_callback(self, callback):
        """ 设置关闭回调，有可能会在_maybe_run_close_callback中被调用（回调无参数）。 """
        self._close_callback = stack_context.wrap(callback)

    def close(self):
        """ 关闭流 """
        if self.socket is not None:
            if any(sys.exc_info()):
                self.error = sys.exc_info()[1]
            if self._read_until_close:         # 若iostream当前的状态是读取直到关闭
                callback = self._read_callback # 则将self._read_callback
                self._read_callback = None     # 取出，
                self._read_until_close = False # 并清除状态，然后将读buffer中的所有剩余内容交给回调
                self._run_callback(callback, self._consume(self._read_buffer_size))
            if self._state is not None:        # 若self._state不为None，则还要从ioloop中remove掉
                self.io_loop.remove_handler(self.socket.fileno())
                self._state = None
            self.socket.close()                # 最后再关闭socket
            self.socket = None
        self._maybe_run_close_callback()       # 并试图调用关闭回调

    def _maybe_run_close_callback(self):
        if (self.socket is None and self._close_callback and self._pending_callbacks == 0):
            # 若存在self._pending_callbacks则表明是异常关闭?
            cb = self._close_callback
            self._close_callback = None
            self._run_callback(cb)

    def reading(self):
        """ 当_read_callback不为None时即在读 """
        return self._read_callback is not None

    def writing(self):
        """ 当_write_buffer不为空时即在写 """
        return bool(self._write_buffer)

    def closed(self):
        """ 当socket为None时为已经关闭 """
        return self.socket is None

    def _handle_events(self, fd, events):
        """ 注册在io_loop上的回调，处理所有事件，委托各_handle_xxx方法处理. """
        if not self.socket:                 # 如果已经关闭，则仅打印warning
            logging.warning("Got events for closed stream %d", fd)
            return
        try:
            if events & self.io_loop.READ:  # 如果发生了读事件，
                self._handle_read()         # 则处理读，
            if not self.socket:             # 处理读之后如果socket已被关闭则不用继续了；
                return

            if events & self.io_loop.WRITE: # 如果发生了写事件，
                if self._connecting:        # 若self._connecting为True则表明当前正在连接中，
                    self._handle_connect()  # 则先完成连接，
                self._handle_write()        # 再处理写，
            if not self.socket:             # 处理写之后如果socket已被关闭则不用继续了；
                return

            if events & self.io_loop.ERROR: # 如果发生了错误，则关闭socket。
                errno = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                self.error = socket.error(errno, os.strerror(errno))
                # 在上面处理读/写事件时可能加入了callback，所以这里不直接关闭
                self.io_loop.add_callback(self.close)
                return

            state = self.io_loop.ERROR
            if self.reading():
                state |= self.io_loop.READ
            if self.writing():
                state |= self.io_loop.WRITE
            if state == self.io_loop.ERROR: # 若当前即不是reading()也不是writing()，
                state |= self.io_loop.READ  # 则默认还是注册为感兴趣读
            if state != self._state:        # 如果状态有变则更新该socket上的监听状态
                assert self._state is not None, "shouldn't happen: _handle_events without self._state"
                self._state = state
                self.io_loop.update_handler(self.socket.fileno(), self._state)
        except Exception:
            logging.error("Uncaught exception, closing connection.", exc_info=True)
            self.close()
            raise

    def _run_callback(self, callback, *args):
        def wrapper():
            self._pending_callbacks -= 1
            try:
                callback(*args)
            except Exception:
                logging.error("Uncaught exception, closing connection.", exc_info=True)
                self.close() # 回调函数时遇到未捕获的异常就直接关闭连接，防止依赖GC可能会用光FD
                raise
            self._maybe_add_error_listener()
        # 以上的wrapper是将callback加入到ioloop中由ioloop来调度执行，把callback推迟到下一轮ioloop的原因是：
        #   1. 避免callback互相调用，调用栈无限增大
        #   2. 为不可重入的互斥体提供一个可预测的执行上下文
        #   3. 确保wrapper中的try/except运行在application的StackContexts之外
        with stack_context.NullContext():
            # stack_context was already captured in callback, we don't need to capture it again for IOStream's wrapper.
            # This is especially important if the callback was pre-wrapped before entry to IOStream
            # (as in HTTPConnection._header_callback), as we could capture and leak the wrong context here.
            self._pending_callbacks += 1
            self.io_loop.add_callback(wrapper)

    def _handle_read(self):
        """ 在io_loop的迭代中处理读事件，由io_loop上的handler自动调用。 """
        try:
            try:
                # 假装有一个pending，防止_read_to_buffer直接把连接关了。
                self._pending_callbacks += 1
                # 第一步：不断地读取网络数据以填充_read_buffer，直到阻塞或者EOF
                while True:
                    if self._read_to_buffer() == 0:
                        break
            finally:
                self._pending_callbacks -= 1
        except Exception:
            logging.warning("error on read", exc_info=True)
            self.close()
            return
        # 第二步：调用_read_from_buffer来完成读操作。如果_read_from_buffer返回False则根据是否已经关闭来调用关闭回调。
        if self._read_from_buffer():
            return
        else:
            self._maybe_run_close_callback()

    def _set_read_callback(self, callback):
        assert not self._read_callback, "Already reading" # 一次只能有一个读工作
        self._read_callback = stack_context.wrap(callback)

    def _try_inline_read(self):
        """ 尝试从缓冲区中完成当前的读操作。用于用户主动的读操作。
        如果读操作可以在未阻塞的情况下完成，则在下一次ioloop中调用读回调；否则在socket上开始监听读。 """
        # 第一步：尝试调用_read_from_buffer完成读操作。如果返回了True则认为操作成功。
        if self._read_from_buffer():
            return
        # 第二步：如果_read_from_buffer返回False(其实此时可能已经读了一些了)，则重新填充_read_buffer。
        self._check_closed()
        try:
            self._pending_callbacks += 1
            while True:
                if self._read_to_buffer() == 0:
                    break
                self._check_closed()
        finally:
            self._pending_callbacks -= 1
        # 第三步：再次尝试从_read_buffer中读取数据并返回。
        if self._read_from_buffer():
            return
        # 第四步：如果_read_buffer还是返回False就: 1.在关闭时调用下关闭回调; 2.否则在io_loop上注册读取通知;
        self._maybe_add_error_listener()

    def _read_from_socket(self):
        """ 从socket中读取数据，并返回读到的字符串。只作为_read_to_buffer方法的辅助过程。 """
        try:
            # 该方法是唯一真正从self.socket中读取数据的方法。
            # 注意到虽然self.socket是non-blocking的，但仍然是同步IO，
            # 即self.socket.recv在无数据时会返回EAGAIN，有数据时直接读取并返回。
            chunk = self.socket.recv(self.read_chunk_size)
        except socket.error, e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            else:
                raise
        if not chunk: # EOF (FIN segment is received)
            self.close()
            return None
        # 若_read_from_socket返回了None，则根据self.closed()来判断是连接关闭还是无数据到达。
        return chunk

    def _read_to_buffer(self):
        """ 把从socket中读到的内容追加到_read_buffer中。返回读到的字节数。该方法会在多处被调用。 """
        try:
            chunk = self._read_from_socket() # 从socket中读
        except socket.error, e: # 这里的异常一定是异常，不会是WOULDBLOCK之类的
            logging.warning("Read error on %d: %s", self.socket.fileno(), e)
            self.close()
            raise
        if chunk is None: # 返回None可能是因为无数据到达，或者另一端关闭了连接（该关闭的已经关闭，这里不用再处理）
            return 0
        self._read_buffer.append(chunk)                     # 把读到的内容追加到_read_buffer中
        self._read_buffer_size += len(chunk)                # 更新_read_buffer_size
        if self._read_buffer_size >= self.max_buffer_size:  # _read_buffer_size一定不能超，否则直接抛异常
            logging.error("Reached maximum read buffer size")
            self.close()
            raise IOError("Reached maximum read buffer size")
        return len(chunk)

    def _read_from_buffer(self):
        """ 根据iostream当前状态试着从读buffer中完成读操作。该方法是主要读取方法，它负责调用当前设置的callback来完成读操作。
        如果读操作顺利完成则返回True。如果读buffer内容不够则返回False。如果没有任何注册的读回调，则该方法会直接返回False。
        该方法只在_handle_read和_try_inline_read中被调用。 """
        if self._streaming_callback is not None and self._read_buffer_size:
            # 如果设置了_streaming_callback则所有的读取块都要交给_streaming_callback处理一遍
            # 如果不是要求读取固定字节数，则把整个_read_buffer都处理了，之后_read_buffer为空;
            bytes_to_consume = self._read_buffer_size
            if self._read_bytes is not None:
                # 如果要求读取固定字节数，则要么把_read_buffer读空，要么读满_read_bytes个字符，使_read_bytes变为0
                bytes_to_consume = min(self._read_bytes, bytes_to_consume)
                self._read_bytes -= bytes_to_consume
            self._run_callback(self._streaming_callback, self._consume(bytes_to_consume)) # 把读的结果丢给_streaming_callback

        # _streaming_callback只可能在read_bytes和read_until_close中被设置：
        # 1.read_bytes: 那么_read_bytes不为None，_read_buffer_size >= self._read_bytes可真可假:
        #   若_read_buffer里还可能有剩余的内容，则返回True(空调一次_read_callback并清空_streaming_callback)；否则返回False。
        # 2.read_until_close: 那么下面的所有if都不成立，直接返回False。这时_streaming_callback不会被清空。

        if self._read_bytes is not None and self._read_buffer_size >= self._read_bytes:
            # 如果是要读取固定的字符数，且该数值小于等于buffer中已经缓存的数量，则直接读取并返回
            num_bytes = self._read_bytes
            callback = self._read_callback
            self._read_callback = None      # 清空_read_callback
            self._streaming_callback = None # 清空了_streaming_callback, 因为这时调用的是read_bytes，再有数据到达时不当作流处理。
            self._read_bytes = None         # 清空_read_bytes
            self._run_callback(callback, self._consume(num_bytes)) # 把_read_bytes个字符丢给callback
            return True
        elif self._read_delimiter is not None:
            if self._read_buffer:
                while True:
                    loc = self._read_buffer[0].find(self._read_delimiter)
                    if loc != -1:
                        callback = self._read_callback
                        delimiter_len = len(self._read_delimiter)
                        self._read_callback = None      # 清空_read_callback
                        self._streaming_callback = None # 清空_streaming_callback
                        self._read_delimiter = None     # 清空_read_delimiter
                        self._run_callback(callback, self._consume(loc + delimiter_len))
                        return True
                    if len(self._read_buffer) == 1:     # 如果_read_buffer所有的chunk都合并了还没找到就退出，返回False
                        break
                    _double_prefix(self._read_buffer)   # 合并_read_buffer的前面几个chunk
        elif self._read_regex is not None:
            if self._read_buffer:
                while True:
                    m = self._read_regex.search(self._read_buffer[0])
                    if m is not None:
                        callback = self._read_callback
                        self._read_callback = None      # 清空_read_callback
                        self._streaming_callback = None # 清空_streaming_callback
                        self._read_regex = None         # 清空_read_regex
                        self._run_callback(callback, self._consume(m.end()))
                        return True
                    if len(self._read_buffer) == 1:     # 如果_read_buffer所有的chunk都合并了还没找到就退出，返回False
                        break
                    _double_prefix(self._read_buffer)   # 合并_read_buffer的前面几个chunk
        return False

    def _handle_connect(self): # 处理连接事件，参见`man 2 connect` EINPROGRESS
        err = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0: # 有错误
            self.error = socket.error(err, os.strerror(err))
            # IOLoop implementations may vary: some of them return an error state before the socket becomes writable,
            # so in that case a connection failure would be handled by the error path in _handle_events instead of here.
            logging.warning("Connect error on fd %d: %s", self.socket.fileno(), errno.errorcode[err])
            self.close()
            return
        if self._connect_callback is not None:
            # 把callback从self._connect_callback中"取出来"之后再调用
            callback = self._connect_callback
            self._connect_callback = None
            self._run_callback(callback)
        self._connecting = False # 完成连接

    def _handle_write(self):
        while self._write_buffer:
            try:
                if not self._write_buffer_frozen:
                    # On windows, socket.send blows up if given a write buffer that's too large, instead of just returning the number
                    # of bytes it was able to process.  Therefore we must not call socket.send with more than 128KB at a time.
                    _merge_prefix(self._write_buffer, 128 * 1024)
                num_bytes = self.socket.send(self._write_buffer[0]) # 每次先试图发送_write_buffer的第一个chunk
                if num_bytes == 0: # 若一个字都没发出去，则下次保持原样重发
                    # With OpenSSL, if we couldn't write the entire buffer, the very same string object must be used on the next call
                    # to send. Therefore we suppress merging the write buffer after an incomplete send. A cleaner solution would be to set
                    # SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER, but this is not yet accessible from python (http://bugs.python.org/issue8240)
                    self._write_buffer_frozen = True
                    break
                self._write_buffer_frozen = False
                _merge_prefix(self._write_buffer, num_bytes) # 这里只能把已经写到网络中的数据给pop掉
                self._write_buffer.popleft()
            except socket.error, e:
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    self._write_buffer_frozen = True
                    break
                else: # 真的错误
                    logging.warning("Write error on %d: %s", self.socket.fileno(), e)
                    self.close()
                    return
        if not self._write_buffer and self._write_callback: # 如果循环退出时数据已经发送完了，则调用_write_callback
            callback = self._write_callback
            self._write_callback = None
            self._run_callback(callback)

    def _consume(self, loc):
        """ 从_read_buffer中消费loc个字符 """
        if loc == 0:
            return b("")
        _merge_prefix(self._read_buffer, loc) # 把前loc个字符调整到_read_buffer[0]的位置
        self._read_buffer_size -= loc         # 调整_read_buffer_size
        return self._read_buffer.popleft()    # 返回前loc个字符

    def _check_closed(self): # 若关闭则直接抛异常
        if not self.socket: # close方法中 self.socket = None
            raise IOError("Stream is closed")

    def _maybe_add_error_listener(self):
        if self._state is None and self._pending_callbacks == 0:
            if self.socket is None: # 关闭时
                self._maybe_run_close_callback()
            else: # 开始时
                self._add_io_state(ioloop.IOLoop.READ)

    def _add_io_state(self, state):
        # 在io_loop上注册事件通知(通过调用io_loop.add_handler)。
        # 事件处理方法统一都走self._handle_events，该方法主要用于改变感兴趣的通知。
        if self.socket is None:
            return
        if self._state is None:        # 之前没注册过，则直接将参数state与上ERROR注册
            self._state = ioloop.IOLoop.ERROR | state
            with stack_context.NullContext():
                self.io_loop.add_handler(self.socket.fileno(), self._handle_events, self._state)
        elif not self._state & state:  # 已经注册过，但参数state与之前注册的不一样，则更新一下
            self._state = self._state | state
            self.io_loop.update_handler(self.socket.fileno(), self._state)


class SSLIOStream(IOStream):
    """A utility class to write to and read from a non-blocking SSL socket.

    If the socket passed to the constructor is already connected,
    it should be wrapped with::

        ssl.wrap_socket(sock, do_handshake_on_connect=False, **kwargs)

    before constructing the SSLIOStream.  Unconnected sockets will be
    wrapped when IOStream.connect is finished.
    """
    def __init__(self, *args, **kwargs):
        """Creates an SSLIOStream.

        If a dictionary is provided as keyword argument ssl_options,
        it will be used as additional keyword arguments to ssl.wrap_socket.
        """
        self._ssl_options = kwargs.pop('ssl_options', {})
        super(SSLIOStream, self).__init__(*args, **kwargs)
        self._ssl_accepting = True
        self._handshake_reading = False
        self._handshake_writing = False
        self._ssl_connect_callback = None

    def reading(self):
        return self._handshake_reading or super(SSLIOStream, self).reading()

    def writing(self):
        return self._handshake_writing or super(SSLIOStream, self).writing()

    def _do_ssl_handshake(self):
        # Based on code from test_ssl.py in the python stdlib
        try:
            self._handshake_reading = False
            self._handshake_writing = False
            self.socket.do_handshake()
        except ssl.SSLError, err:
            if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                self._handshake_reading = True
                return
            elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self._handshake_writing = True
                return
            elif err.args[0] in (ssl.SSL_ERROR_EOF,
                                 ssl.SSL_ERROR_ZERO_RETURN):
                return self.close()
            elif err.args[0] == ssl.SSL_ERROR_SSL:
                try:
                    peer = self.socket.getpeername()
                except:
                    peer = '(not connected)'
                logging.warning("SSL Error on %d %s: %s",
                                self.socket.fileno(), peer, err)
                return self.close()
            raise
        except socket.error, err:
            if err.args[0] in (errno.ECONNABORTED, errno.ECONNRESET):
                return self.close()
        else:
            self._ssl_accepting = False
            if self._ssl_connect_callback is not None:
                callback = self._ssl_connect_callback
                self._ssl_connect_callback = None
                self._run_callback(callback)

    def _handle_read(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super(SSLIOStream, self)._handle_read()

    def _handle_write(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super(SSLIOStream, self)._handle_write()

    def connect(self, address, callback=None):
        # Save the user's callback and run it after the ssl handshake
        # has completed.
        self._ssl_connect_callback = callback
        super(SSLIOStream, self).connect(address, callback=None)

    def _handle_connect(self):
        # When the connection is complete, wrap the socket for SSL
        # traffic.  Note that we do this by overriding _handle_connect
        # instead of by passing a callback to super().connect because
        # user callbacks are enqueued asynchronously on the IOLoop,
        # but since _handle_events calls _handle_connect immediately
        # followed by _handle_write we need this to be synchronous.
        self.socket = ssl.wrap_socket(self.socket,
                                      do_handshake_on_connect=False,
                                      **self._ssl_options)
        super(SSLIOStream, self)._handle_connect()

    def _read_from_socket(self):
        if self._ssl_accepting:
            # If the handshake hasn't finished yet, there can't be anything
            # to read (attempting to read may or may not raise an exception
            # depending on the SSL version)
            return None
        try:
            # SSLSocket objects have both a read() and recv() method,
            # while regular sockets only have recv().
            # The recv() method blocks (at least in python 2.6) if it is
            # called when there is nothing to read, so we have to use
            # read() instead.
            chunk = self.socket.read(self.read_chunk_size)
        except ssl.SSLError, e:
            # SSLError is a subclass of socket.error, so this except
            # block must come first.
            if e.args[0] == ssl.SSL_ERROR_WANT_READ:
                return None
            else:
                raise
        except socket.error, e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            else:
                raise
        if not chunk:
            self.close()
            return None
        return chunk


def _double_prefix(deque):
    """Grow by doubling, but don't split the second chunk just because the first one is small. """
    new_len = max(len(deque[0])*2, (len(deque[0])+len(deque[1])))
    _merge_prefix(deque, new_len)


def _merge_prefix(deque, size):
    """Replace the first entries in a deque of strings with a single string of up to size bytes.

    >>> d = collections.deque(['abc', 'de', 'fghi', 'j'])
    >>> _merge_prefix(d, 5); print d
    deque(['abcde', 'fghi', 'j'])

    Strings will be split as necessary to reach the desired size.
    >>> _merge_prefix(d, 7); print d
    deque(['abcdefg', 'hi', 'j'])

    >>> _merge_prefix(d, 3); print d
    deque(['abc', 'defg', 'hi', 'j'])

    >>> _merge_prefix(d, 100); print d
    deque(['abcdefghij'])
    """
    if len(deque) == 1 and len(deque[0]) <= size:
        return
    prefix = []
    remaining = size
    while deque and remaining > 0:
        chunk = deque.popleft()
        if len(chunk) > remaining:
            deque.appendleft(chunk[remaining:])
            chunk = chunk[:remaining]
        prefix.append(chunk)
        remaining -= len(chunk)
    # This data structure normally just contains byte strings, but
    # the unittest gets messy if it doesn't use the default str() type,
    # so do the merge based on the type of data that's actually present.
    if prefix:
        deque.appendleft(type(prefix[0])().join(prefix))
    if not deque:
        deque.appendleft(b(""))


def doctests():
    import doctest
    return doctest.DocTestSuite()
