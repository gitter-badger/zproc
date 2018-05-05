import pickle
import sys
from collections import defaultdict
from collections import deque
from pathlib import Path
# from time import sleep
from uuid import uuid1

import zmq
from tblib import pickling_support

pickling_support.install()


class MSGS:
    action = 'action'

    args = 'args'
    kwargs = 'kwargs'

    testfn = 'callable'

    state = 'state'
    key = 'key'
    keys = 'keys'
    value = 'value'

    globals = 'globals'

    attr_name = 'item'


class ACTIONS:
    """A map of some ACTIONS to methods in ZProc Server class"""

    lock_state = 'lock_state'
    unlock_state = 'unlock_state'

    get_state = 'send_state'

    get_state_callable = 'get_state_callable'
    get_state_attr = 'get_state_attr'

    change_handler = 'add_change_handler'
    val_change_handler = 'add_val_change_handler'
    condition_handler = 'add_condition_handler'
    equals_handler = 'add_equals_handler'


ipc_base_dir = Path.home().joinpath('.tmp')

if not ipc_base_dir.exists():
    ipc_base_dir.mkdir()


def get_random_ipc():
    return get_ipc_path(uuid1())


def get_ipc_path(uuid):
    return 'ipc://' + str(ipc_base_dir.joinpath(str(uuid)))


class Queue:
    def __init__(self):
        self._deque = deque()

    def put(self, something):
        self._deque.appendleft(something)

    def get(self):
        self._deque.pop()

    def clear(self):
        self._deque.clear()

    def drain(self):
        # create a copy of the deque, to prevent shit-storms while iterating :)
        iter_deque = self._deque.copy()
        self.clear()

        while True:
            try:
                yield iter_deque.pop()
            except IndexError:
                break


ACTIONS_THAT_MUTATE = (
    '__setitem__',
    '__delitem__',
    'setdefault',
    'pop',
    'popitem',
    'clear',
    'update',
)


class ZProcServerException:
    def __init__(self):
        self.exc = sys.exc_info()

    def reraise(self):
        raise self.exc[0].with_traceback(self.exc[1], self.exc[2])

    def __str__(self):
        return str(self.exc)


class ZProcServer:
    def __init__(self, bind_ipc_path):
        self.state = {}

        self.state_lock_ident = None

        self.condition_handlers = Queue()
        self.equals_handlers = Queue()
        self.val_change_handlers = defaultdict(Queue)
        self.change_handlers = defaultdict(Queue)

        # init zmq socket
        self.ctx = zmq.Context()
        self.sock = self.ctx.socket(zmq.ROUTER)
        self.sock.bind(bind_ipc_path)

    def wait_req(self):
        """wait for client to send a request"""
        ident, msg = self.sock.recv_multipart()
        return ident, pickle.loads(msg)

    def reply(self, ident, msg):
        """reply to a client (with said identity) and msg"""
        return self.sock.send_multipart([ident, pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)])

    def push(self, ipc_path, msg):
        """push to an ipc_path, the given msg"""
        sock = self.ctx.socket(zmq.PUSH)
        sock.bind(ipc_path)
        sock.send_pyobj(msg, protocol=pickle.HIGHEST_PROTOCOL)
        sock.close()

    def get_keys_cmp(self, state_keys):
        return [self.state.get(state_key) for state_key in state_keys]

    def send_state(self, ident, msg=None):
        """Sends the state back to a process"""
        self.reply(ident, self.state)

    def lock_state(self, ident, msg=None):
        ipc_path = get_random_ipc()
        self.reply(ident, (ipc_path, self.state))

        ctx = zmq.Context()
        sock = ctx.socket(zmq.PULL)
        sock.bind(ipc_path)
        locked_state = sock.recv_pyobj()
        sock.close()

        # update state
        if locked_state != self.state:
            self.state = locked_state
            self.resolve_all_handlers()

    def get_state_callable(self, ident, msg):
        args, kwargs = msg.get(MSGS.args, ()), msg.get(MSGS.kwargs, {})
        attr_name = msg[MSGS.attr_name]

        can_mutate = attr_name in ACTIONS_THAT_MUTATE
        if can_mutate:
            old = self.state.copy()
        else:
            old = None

        result = getattr(self.state, attr_name)(*args, **kwargs)

        self.reply(ident, result)

        if can_mutate and old != self.state:
            self.resolve_all_handlers()

    def get_state_attr(self, ident, msg):
        self.reply(ident, getattr(self.state, msg[MSGS.attr_name]))

    # on change handler

    def add_change_handler(self, ident, msg):
        ipc_path = get_random_ipc()
        self.reply(ident, ipc_path)

        keys = msg[MSGS.keys]

        if len(keys):
            self.change_handlers[keys].put(
                (ipc_path, self.get_keys_cmp(keys))
            )
        else:
            self.change_handlers['_any_'].put(
                (ipc_path, self.state.copy())
            )

        self.resolve_change_handlers()

    def resolve_change_handlers(self):
        for keys, change_handler_queue in self.change_handlers.items():
            if keys == '_any_':
                new = self.state
            else:
                new = self.get_keys_cmp(keys)

            to_put_back = []

            for ipc_path, old in change_handler_queue.drain():
                if old != new:
                    self.push(ipc_path, self.state)
                else:
                    to_put_back.append((ipc_path, old))

            for i in to_put_back:
                self.change_handlers[keys].put(i)

    # on val change handler

    def add_val_change_handler(self, ident, msg):
        ipc_path = get_random_ipc()
        self.reply(ident, ipc_path)

        key = msg[MSGS.key]

        try:
            old_value = msg[MSGS.value]
        except KeyError:
            old_value = self.state.get(key)

        self.val_change_handlers[key].put(
            (ipc_path, old_value)
        )

        self.resolve_val_change_handlers()

    def resolve_val_change_handlers(self):
        for key, handler_list in self.val_change_handlers.items():
            new = self.state.get(key)

            to_put_back = []

            for ipc_path, old in handler_list.drain():
                if old != new:
                    self.push(ipc_path, self.state.get(key))
                else:
                    to_put_back.append((ipc_path, old))

            for i in to_put_back:
                self.val_change_handlers[key].put(i)

    # on equal handler

    def add_equals_handler(self, ident, msg):
        ipc_path = get_random_ipc()
        self.reply(ident, ipc_path)

        self.equals_handlers.put(
            (msg[MSGS.key], msg[MSGS.value], ipc_path)
        )

        self.resolve_equals_handler()

    def resolve_equals_handler(self):
        to_put_back = []

        for key, value, ipc_path in self.equals_handlers.drain():
            if self.state.get(key) == value:
                self.push(ipc_path, True)
            else:
                to_put_back.append((key, value, ipc_path))

        for i in to_put_back:
            self.equals_handlers.put(i)

    # condition handler

    def add_condition_handler(self, ident, msg):
        ipc_path = get_random_ipc()
        self.reply(ident, ipc_path)

        self.condition_handlers.put((
            ipc_path,
            # FunctionType(msg[MSGS.testfn], globals()),
            msg[MSGS.testfn],
            msg[MSGS.args],
            msg[MSGS.kwargs]
        ))

        self.resolve_condition_handlers()

    def resolve_condition_handlers(self):
        to_put_back = []
        for ipc_path, test_fn, args, kwargs in self.condition_handlers.drain():
            if test_fn(self.state, *args, **kwargs):
                self.push(ipc_path, self.state)
            else:
                to_put_back.append((ipc_path, test_fn, args, kwargs))

        for i in to_put_back:
            self.condition_handlers.put(i)

    def resolve_all_handlers(self):
        self.resolve_change_handlers()
        self.resolve_condition_handlers()
        self.resolve_val_change_handlers()
        self.resolve_equals_handler()


def state_server(bind_ipc_path: str):
    # sleep(1)
    server = ZProcServer(bind_ipc_path)

    # server mainloop
    while True:
        ident, msg = server.wait_req()
        # print(msg, ident)
        try:
            action = MSGS.action
            getattr(server, msg[action])(ident, msg)
        except Exception:
            server.reply(ident, ZProcServerException())
