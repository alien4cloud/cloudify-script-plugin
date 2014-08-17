########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.


import socket
import subprocess
import os
import sys
import time
import threading
from StringIO import StringIO

from cloudify.decorators import operation
from cloudify.exceptions import NonRecoverableError

import eval_env
from ctx_proxy import (UnixCtxProxy,
                       TCPCtxProxy,
                       HTTPCtxProxy,
                       StubCtxProxy,
                       CTX_SOCKET_URL)

try:
    import zmq  # noqa
    HAS_ZMQ = True
except ImportError:
    HAS_ZMQ = False

IS_WINDOWS = os.name == 'nt'


@operation
def run(ctx, **kwargs):
    script_path = get_script_to_run(ctx)
    if script_path:

        def returns(value):
            ctx._return_value = value
        ctx.returns = returns
        ctx._return_value = None

        eval_python = ctx.properties.get('process', {}).get('eval_python')
        if eval_python is True or (script_path.endswith('.py') and
                                   eval_python is not False):
            eval_script(script_path, ctx)
        else:
            execute(script_path, ctx)

        return ctx._return_value


def get_script_to_run(ctx):
    script_path = ctx.properties.get('script_path')
    if script_path:
        script_path = ctx.download_resource(script_path)
    elif 'scripts' in ctx.properties:
        operation_simple_name = ctx.operation.split('.')[-1:].pop()
        scripts = ctx.properties['scripts']
        if operation_simple_name not in scripts:
            ctx.logger.info("No script mapping found for operation {0}. "
                            "Nothing to do.".format(operation_simple_name))
            return None
        script_path = ctx.download_resource(scripts[operation_simple_name])
    if not script_path:
        raise NonRecoverableError('No script to run was defined either in '
                                  '"script_path" node property on under '
                                  '"scripts/{operation}" node property')
    os.chmod(script_path, 0755)
    return script_path


def execute(script_path, ctx):
    on_posix = 'posix' in sys.builtin_module_names
    process_config = ctx.properties.get('process', {})

    proxy = start_ctx_proxy(ctx, process_config)

    env = os.environ.copy()
    process_env = process_config.get('env', {})
    env.update(process_env)
    env[CTX_SOCKET_URL] = proxy.socket_url

    cwd = process_config.get('cwd')

    command_prefix = process_config.get('command_prefix')
    if command_prefix:
        command = '{} {}'.format(command_prefix, script_path)
    else:
        command = script_path

    ctx.logger.info('Executing: {}'.format(command))

    process = subprocess.Popen(command,
                               shell=True,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               env=env,
                               cwd=cwd,
                               bufsize=1,
                               close_fds=on_posix)

    return_code = None

    stdout_consumer = OutputConsumer(process.stdout)
    stderr_consumer = OutputConsumer(process.stderr)

    while True:
        process_ctx_request(proxy)
        return_code = process.poll()
        if return_code is not None:
            break
        time.sleep(0.1)

    proxy.close()
    stdout_consumer.join()
    stderr_consumer.join()

    ctx.logger.info('Execution done (return_code={}): {}'
                    .format(return_code, command))

    if return_code != 0:
        raise ProcessException(command,
                               return_code,
                               stdout_consumer.buffer.getvalue(),
                               stderr_consumer.buffer.getvalue())


def start_ctx_proxy(ctx, process_config):
    ctx_proxy_type = process_config.get('ctx_proxy_type')
    if not ctx_proxy_type or ctx_proxy_type == 'auto':
        if HAS_ZMQ:
            if IS_WINDOWS:
                return TCPCtxProxy(ctx, port=get_unused_port())
            else:
                return UnixCtxProxy(ctx)
        else:
            return HTTPCtxProxy(ctx, port=get_unused_port())
    elif ctx_proxy_type == 'unix':
        return UnixCtxProxy(ctx)
    elif ctx_proxy_type == 'tcp':
        return TCPCtxProxy(ctx, port=get_unused_port())
    elif ctx_proxy_type == 'http':
        return HTTPCtxProxy(ctx, port=get_unused_port())
    elif ctx_proxy_type == 'none':
        return StubCtxProxy()
    else:
        raise NonRecoverableError('Unsupported proxy type: {}'
                                  .format(ctx_proxy_type))


def process_ctx_request(proxy):
    if isinstance(proxy, StubCtxProxy):
        return
    if isinstance(proxy, HTTPCtxProxy):
        return
    proxy.poll_and_process(timeout=0)


def get_unused_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    _, port = sock.getsockname()
    sock.close()
    return port


def eval_script(script_path, ctx):
    eval_globals = eval_env.setup_env_and_globals(ctx, script_path)
    execfile(script_path, eval_globals)


class OutputConsumer(object):

    def __init__(self, out):
        self.out = out
        self.buffer = StringIO()
        self.consumer = threading.Thread(target=self.consume_output)
        self.consumer.daemon = True
        self.consumer.start()

    def consume_output(self):
        for line in iter(self.out.readline, b''):
            self.buffer.write(line)
        self.out.close()

    def join(self):
        self.consumer.join()


class ProcessException(Exception):

    def __init__(self, command, exit_code, stdout, stderr):
        super(ProcessException, self).__init__(stderr)
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr