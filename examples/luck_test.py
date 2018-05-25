"""
A random sync test, by generating random numbers into the state.

# Expected output

num gen: 0.9335641557984383

...<bunch o crazy numbers>...

listener: foo is between 0.6 and 0.61, so I awake
num gen: <some crazy number>
listener: I set STOP to True
listener: exit
num gen: STOP was set to True, so lets exit
num gen: exit

"""

import random

import zproc


# random num generator process
def random_num_gen(state: zproc.ZeroState):
    while True:
        num = random.random()
        state['foo'] = num

        print('num gen:', num)

        if state.get('STOP'):
            print('num gen: STOP was set to True, so lets exit')
            break

    print('num gen: exit')


def foo_between(state, props):
    return props[0] < state.get('foo') < props[1]


# num listener process
def num_listener(state: zproc.ZeroState, props):
    state.get_when(foo_between, props)  # blocks until foo is between the specified range

    print('listener: foo is between {0} and {1}, so I awake'.format(props[0], props[1]))

    state['STOP'] = True
    print('listener: I set STOP to True')

    print('listener: exit')


if __name__ == '__main__':
    ctx = zproc.Context(background=True)  # create a context for us to work with

    # give the context some processes to work with
    # also give some props to the num listener
    ctx.process_factory(random_num_gen, num_listener, props=(0.6, 0.601))

    print(ctx.state)