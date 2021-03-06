from functools import wraps

import re
import redis
from opentracing.ext import tags as ext_tags
import opentracing_instrumentation
import opentracing_instrumentation.utils

# regex to match an ipv4 address
IPV4_RE = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')

_ARGUMENT_LENGTH_LIMIT = 128

g_trace_prefix = None
g_trace_all_classes = True

def setup_tracing(trace_all_classes=True, prefix='Redis'):
    '''
    Set our tracer for Redis. Tracer objects from the
    OpenTracing django/flask/pyramid libraries can be passed as well.

    :param trace_all_classes: If True, Redis clients and pipelines
        are automatically traced. Else, explicit tracing on them
        is required.
    :param prefix: The prefix for the operation name, if any.
        By default it is set to 'Redis'.
    '''
    global g_trace_all_classes, g_trace_prefix

    g_trace_all_classes = trace_all_classes
    g_trace_prefix = prefix

    if g_trace_all_classes:
        _patch_redis_classes()

def trace_client(client):
    '''
    Marks a client to be traced. All commands and pipelines executed
    through this client will be traced.

    :param client: the Redis client object.
    '''
    _patch_client(client)

def trace_pipeline(pipe):
    '''
    Marks a pipeline to be traced.

    :param pipe: the Redis pipeline object to be traced.
    If executed as a transaction, the commands will appear
    under a single 'MULTI' operation.
    '''
    _patch_pipe_execute(pipe)

def trace_pubsub(pubsub):
    '''
    Marks a pubsub object to be traced.

    :param pubsub: the Redis pubsub object to be traced.
    Incoming messages through get_message(), listen() and
    run_in_thread() will appear with an operation named 'SUB'.
    Commands executed on this object through execute_command()
    will be traced too with their respective command name.
    '''
    _patch_pubsub(pubsub)

def _get_operation_name(operation_name):
    if g_trace_prefix is not None:
        operation_name = '{0}/{1}'.format(g_trace_prefix, operation_name)

    return operation_name

def _truncate(val):
    val = str(val)
    try:
        val = unicode(val)
        if len(val) > _ARGUMENT_LENGTH_LIMIT:
            val = val[:_ARGUMENT_LENGTH_LIMIT]
    except:
        val = u'{bytes}'
    return val

def _normalize_stmt(args):
    return ' '.join([_truncate(arg) for arg in args])

def _normalize_stmts(command_stack):
    commands = [_normalize_stmt(command[0]) for command in command_stack]
    return ';'.join(commands)

def _set_base_span_tags(self, span, stmt):
    if self:
        peer_tags = _peer_tags(self)
        for tag_key, tag_val in peer_tags:
            span.set_tag(tag_key, tag_val)

    span.set_tag('component', 'redis-py')
    span.set_tag('db.type', 'redis')
    span.set_tag('db.statement', stmt)
    span.set_tag('span.kind', 'client')

def _peer_tags(self):
    """Fetch the peer host/port tags for opentracing."""

    # from https://github.com/hsheth2/opentracing-python-instrumentation/blob/8fb509a16d60b05938ca33c31e9a007467b9e65d/opentracing_instrumentation/client_hooks/strict_redis.py#L48-L68
    if hasattr(self, 'connection_pool'):
        connection_pool = self.connection_pool
    else:
        connection_pool = self

    peer_tags = []
    conn_info = connection_pool.connection_kwargs
    host = conn_info.get('host')
    if host:
        if IPV4_RE.match(host):
            peer_tags.append((ext_tags.PEER_HOST_IPV4, host))
        else:
            peer_tags.append((ext_tags.PEER_HOSTNAME, host))
    port = conn_info.get('port')
    if port:
        peer_tags.append((ext_tags.PEER_PORT, port))
    return peer_tags

def _patch_redis_classes():
    # Patch the outgoing commands.
    _patch_obj_execute_command(redis.StrictRedis, True)
    
    # Patch the created pipelines.
    pipeline_method = redis.StrictRedis.pipeline

    @wraps(pipeline_method)
    def tracing_pipeline(self, transaction=True, shard_hint=None):
        pipe = pipeline_method(self, transaction, shard_hint)
        _patch_pipe_execute(pipe)
        return pipe

    redis.StrictRedis.pipeline = tracing_pipeline

    # Patch the created pubsubs.
    pubsub_method = redis.StrictRedis.pubsub

    @wraps(pubsub_method)
    def tracing_pubsub(self, **kwargs):
        pubsub = pubsub_method(self, **kwargs)
        _patch_pubsub(pubsub)
        return pubsub

    redis.StrictRedis.pubsub = tracing_pubsub

def _patch_client(client):
    # Patch the outgoing commands.
    _patch_obj_execute_command(client)

    # Patch the created pipelines.
    pipeline_method = client.pipeline

    @wraps(pipeline_method)
    def tracing_pipeline(transaction=True, shard_hint=None):
        pipe = pipeline_method(transaction, shard_hint)
        _patch_pipe_execute(pipe)
        return pipe

    client.pipeline = tracing_pipeline

    #Patch the created pubsubs.
    pubsub_method = client.pubsub

    @wraps(pubsub_method)
    def tracing_pubsub(**kwargs):
        pubsub = pubsub_method(**kwargs)
        _patch_pubsub(pubsub)
        return pubsub

    client.pubsub = tracing_pubsub


def _patch_pipe_execute(pipe):
    # Patch the execute() method.
    execute_method = pipe.execute

    @wraps(execute_method)
    def tracing_execute(raise_on_error=True):
        if not pipe.command_stack:
            # Nothing to process/handle.
            return execute_method(raise_on_error=raise_on_error)

        span = opentracing_instrumentation.utils.start_child_span(
            operation_name=_get_operation_name('MULTI'),
            parent=opentracing_instrumentation.get_current_span())
        _set_base_span_tags(pipe, span, _normalize_stmts(pipe.command_stack))

        try:
            res = execute_method(raise_on_error=raise_on_error)
        except Exception as exc:
            span.set_tag('error', 'true')
            span.set_tag('error.object', exc)
            raise
        finally:
            span.finish()

        return res

    pipe.execute = tracing_execute

    # Patch the immediate_execute_command() method.
    immediate_execute_method = pipe.immediate_execute_command
    @wraps(immediate_execute_method)
    def tracing_immediate_execute_command(*args, **options):
        command = args[0]
        span = opentracing_instrumentation.utils.start_child_span(
            operation_name=_get_operation_name(command),
            parent=opentracing_instrumentation.get_current_span())
        _set_base_span_tags(pipe, span, _normalize_stmt(args))

        try:
            res = immediate_execute_method(*args, **options)
        except Exception as exc:
            span.set_tag('error', 'true')
            span.set_tag('error.object', exc)
        finally:
            span.finish()

    pipe.immediate_execute_command = tracing_immediate_execute_command

def _patch_pubsub(pubsub):
    _patch_pubsub_parse_response(pubsub)
    _patch_obj_execute_command(pubsub)

def _patch_pubsub_parse_response(pubsub):
    # Patch the parse_response() method.
    parse_response_method = pubsub.parse_response

    @wraps(parse_response_method)
    def tracing_parse_response(block=True, timeout=0):
        span = opentracing_instrumentation.utils.start_child_span(
            operation_name=_get_operation_name('SUB'),
            parent=opentracing_instrumentation.get_current_span())
        _set_base_span_tags(pubsub, span, '')

        try:
            rv = parse_response_method(block=block, timeout=timeout)
        except Exception as exc:
            span.set_tag('error', 'true')
            span.set_tag('error.object', exc)
            raise
        finally:
            span.finish()

        return rv

    pubsub.parse_response = tracing_parse_response

def _patch_obj_execute_command(redis_obj, is_klass=False):
    execute_command_method = redis_obj.execute_command

    @wraps(execute_command_method)
    def tracing_execute_command(*args, **kwargs):
        if is_klass: 
            # Unbound method, we will get 'self' in args.
            self = args[0]
            reported_args = args[1:]
        else:
            self = None
            reported_args = args

        command = reported_args[0]

        span = opentracing_instrumentation.utils.start_child_span(
            operation_name=_get_operation_name(command),
            parent=opentracing_instrumentation.get_current_span())
        _set_base_span_tags(self, span, _normalize_stmt(reported_args))

        try:
            rv = execute_command_method(*args, **kwargs)
        except Exception as exc:
            span.set_tag('error', 'true')
            span.set_tag('error.object', exc)
            raise
        finally:
            span.finish()

        return rv

    redis_obj.execute_command = tracing_execute_command
