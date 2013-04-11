import sys
import logging
from collections import deque
from datetime import datetime, timedelta
from functools import partial

from pulsar import async, EMPTY_TUPLE, EMPTY_DICT, get_actor, log_failure,\
                    maybe_failure, is_failure
from pulsar.utils.importer import import_module, import_modules
from pulsar.utils.pep import itervalues, iteritems
from pulsar.apps.tasks.models import registry
from pulsar.apps.tasks import states
from pulsar.utils.httpurl import parse_qs, urlsplit
from pulsar.utils.timeutils import remaining, timedelta_seconds
from pulsar.utils.log import LocalMixin, local_property 

__all__ = ['Task', 'TaskBackend', 'getbe']


LOGGER = logging.getLogger('pulsar.tasks')


def parse_backend(backend):
    """Converts the "backend" into the database connection parameters.
It returns a (scheme, host, params) tuple."""
    r = urlsplit(backend)
    scheme, host = r.scheme, r.netloc
    path, query = r.path, r.query
    if path and not query:
        query, path = path, ''
        if query:
            if query.find('?'):
                path = query
            else:
                query = query[1:]
    params = parse_qs(query) if query else {}
    return scheme, host, params


def _getdb(scheme, host, params):
    try:
        module = import_module('pulsar.apps.tasks.backends.%s' % scheme)
    except ImportError:
        module = import_module(scheme)
    return getattr(module, 'TaskBackend')(scheme, host, **params)
    
    
def getbe(backend=None, **kwargs):
    if isinstance(backend, TaskBackend):
        return backend
    backend = backend or 'local://'
    scheme, address, params = parse_backend(backend)
    params.update(kwargs)
    if 'timeout' in params:
        params['timeout'] = int(params['timeout'])
    return _getdb(scheme, address, params)

def get_datetime(expiry, start):
    if isinstance(expiry, datetime):
        return expiry
    elif isinstance(expiry, timedelta):
        return start + expiry
    else:
        return datetime.fromtimestamp(expiry)


class TaskConsumer(object):
    '''A context manager for consuming tasks.

.. attribute:: task

    the :class:`Task` being consumed.

.. attribute:: job

    the :class:`Job` which generated the :attr:`task`.

.. attribute:: worker

    the :class:`pulsar.apps.Worker` running the process.
    
.. attribute:: backend

    give access to the :class:`TaskBackend`.
'''
    def __init__(self, backend, task, job):
        self.backend = backend
        self.worker = get_actor()
        self.job = job
        self.task = task
    
    
class Task(object):
    '''Interface for tasks which are produced by
:ref:`jobs or periodic jobs <apps-taskqueue-job>`.

.. attribute:: id

    :class:`Task` unique id.

.. attribute:: name

    :class:`Job` name.

.. attribute:: status

    The current :ref:`status string <task-state>` of task.

.. attribute:: time_executed

    date time when the task was executed.

.. attribute:: time_start

    date-time when the task calculation has started.

.. attribute:: time_end

    date-time when the task has finished.

.. attribute:: expiry

    optional date-time indicating when the task should expire.

.. attribute:: timeout

    A datetime or ``None`` indicating whether a timeout has occurred.

.. attribute:: from_task

    Optional :attr:`Task.id` for the :class:`Task` which queued
    this :class:`Task`. This is a usuful for monitoring the creation
    of tasks within other tasks.
'''
    stack_trace = None
    def __init__(self, id, name=None, time_executed=None,
                 expiry=None, args=None, kwargs=None, 
                 status=None, from_task=None, **params):
        self.id = id
        self.name = name
        self.time_executed = time_executed
        self.from_task = from_task
        self.time_started = None
        self.time_ended = None
        self.expiry = expiry
        self.args = args
        self.kwargs = kwargs
        self.status = status
        self.params = params

    @property
    def status_code(self):
        '''Integer indicating :attr:`status` precedence.
Lower number higher precedence.'''
        return states.PRECEDENCE_MAPPING.get(self.status, states.UNKNOWN_STATE)

    def done(self):
        '''Return ``True`` if the :class:`Task` has finshed
(its status is one of :ref:`READY_STATES <task-state>`).'''
        return self.status in READY_STATES

    def execute2start(self):
        if self.time_start:
            return self.time_start - self.time_executed

    def execute2end(self):
        if self.time_end:
            return self.time_end - self.time_executed

    def duration(self):
        '''The :class:`Task` duration. Only available if the task status is in
:attr:`FULL_RUN_STATES`.'''
        if self.time_end and self.time_start:
            return self.time_end - self.time_start

    def tojson(self):
        '''Convert the task instance into a JSON-serializable dictionary.'''
        return self.__dict__.copy()

    def ack(self):
        return self
    
    
class TaskBackend(LocalMixin):
    '''Base class for :class:`Task` backends.
The backend is responsible for creating tasks and put them into
the distributed :attr:`queue`. It also schedule the run of periodic tasks if
enabled to do so.
This class is the main driver of tasks and task scheduling.

.. attribute:: name

    The name of the task queue application served by this :class:`TaskBackend`.
    
.. attribute:: task_path

    List of paths where to upload :ref:`jobs <app-taskqueue-job>` which
    are factory of tasks.
    
.. attribute:: schedule_periodic

    `True` if this :class:`Scheduler` can schedule periodic tasks.
'''
    def __init__(self, scheme, host, name=None, task_paths=None,
                 schedule_periodic=False, backlog=1, **params):
        self.scheme = scheme
        self.host = host
        self.params = params
        self.name = name
        self.task_paths = task_paths
        self.backlog = backlog
        self.local.schedule_periodic = schedule_periodic
        self.next_run = datetime.now()
        
    @property
    def schedule_periodic(self):
        return self.local.schedule_periodic
    
    @property
    def concurrent_tasks(self):
        return len(self.task_queue)
    
    @local_property
    def concurrent_requests(self):
        return 0
    
    @local_property
    def entries(self):
        return self._setup_schedule()
    
    @local_property
    def task_queue(self):
        return deque()
    
    @local_property
    def registry(self):
        if self.task_paths:
            import_modules(self.task_paths)
        return registry
    
    def run(self, jobname, *args, **kwargs):
        '''A shortcut for :meth:`run_job` without task meta parameters'''
        return self.run_job(jobname, args, kwargs)
    
    def run_job(self, jobname, targs=None, tkwargs=None, **meta_params):
        '''Create a new :ref:`task <apps-taskqueue-task>` which may or
may not be queued. This method returns a :ref:`coroutine <coroutine>`.
If *jobname* is not a valid :attr:`pulsar.apps.tasks.models.Job.name`,
a ``TaskNotAvailable`` exception occurs.

:parameter jobname: the name of a :class:`Job` registered
    with the :class:`TaskQueue` application.
:parameter targs: optional tuple used for the positional arguments in the
    task callable.
:parameter tkwargs: optional dictionary used for the key-valued arguments
    in the task callable.
:parameter meta_params: Additional parameters to be passed to the :class:`Task`
    constructor (not its callable function).
:return: a :ref:`task <apps-taskqueue-task>`.'''
        return self._run_job(jobname, targs, tkwargs, meta_params)\
                   .add_errback(log_failure)
        
    @async()
    def queue_task(self, task_id_or_task):
        id = task_id_or_task
        if isinstance(task_id_or_task, Task):
            id = task_id_or_task.id
        self.put_task(id)
        
    def create_task_id(self, job, *args, **kwargs):
        '''Create a :class:`Task` id from *job*, positional arguments *args*
and key-valued arguments *kwargs*.'''
        return job.make_task_id(args, kwargs)
        
    def create_task(self, jobname, targs=None, tkwargs=None, expiry=None,
                    **params):
        '''Create a new :ref:`task <apps-taskqueue-task>`
        
:param jobname: the name of job which create the task.
:param targs: task positional arguments (a ``tuple`` or ``None``).
:param tkwargs: task key-valued arguments (a ``dict`` or ``None``).
:return: The :attr:`Task.id` or ``None`` if no task was created.
'''
        if jobname in self.registry:
            job = self.registry[jobname]
            targs = targs or EMPTY_TUPLE
            tkwargs = tkwargs or EMPTY_DICT
            task_id = self.create_task_id(job, targs, tkwargs)
            task = yield self.get_task(task_id)
            if task:
                # the task with id is already available
                if task.done():
                    task = self.handle_task_done(task)
            if task:
                LOGGER.debug('Task %s already requested, abort.', task)
                yield None
            else:
                if self.entries and job.name in self.entries:
                    self.entries[job.name].next()
                time_executed = datetime.now()
                if expiry is not None:
                    expiry = get_datetime(expiry, time_executed)
                elif job.timeout:
                    expiry = get_datetime(job.timeout, time_executed)
                yield self.save_task(task_id, name=job.name,
                                     time_executed=time_executed,
                                     expiry=expiry, args=targs, kwargs=tkwargs,
                                     status=states.PENDING, **params)
        else:
            raise TaskNotAvailable(jobname)
    
    @async()
    def may_pool_task(self):
        '''Called at every loop in the worker IO loop, it pool a new task
if possible and add it to the queue of tasks consumed by the worker
CPU-bound thread.'''
        thread_pool = get_actor().thread_pool
        if not thread_pool:
            LOGGER.warning('No thread pool, cannot poll tasks.')
        elif self.concurrent_requests < self.backlog:
            task = yield self.get_task()
            if task:
                self.local.concurrent_requests += 1
                thread_pool.apply(self.execute_task, (task,))
            
    @async(max_errors=0)
    def execute_task(self, task):
        '''Asynchronous execution of a task'''
        job = self.registry[task.name]
        timeout = datetime.now()
        result = None
        if task.status_code > states.PRECEDENCE_MAPPING[states.STARTED]:
            if task.expiry and timeout > task.expiry:
                #
                # TIMEOUT
                task = yield self.save_task(task.id, status=states.REVOKED,
                                            time_ended=timeout)
                yield self.on_timeout_task(task)
            else:
                #
                # START
                task = yield self.save_task(task.id, status=states.STARTED,
                                            time_started=timeout)
                yield self.on_start_task(task)
                consumer = TaskConsumer(self, task, job)
                try:
                    result = yield job(consumer, *task.args, **task.kwargs)
                except:
                    result = maybe_failure(sys.exc_info())
                time_ended = datetime.now()
                if is_failure(result):
                    result.log()
                    exception = result.trace[1]
                    status = states.FAILURE
                    result = str(result)
                    # If the status is STARTED this is a succesful task
                elif task.status == STARTED:
                    status = states.SUCCESS
                task = yield self.save(task.id, status=status,
                                       time_ended=time_ended, result=result)
            yield self.on_finish_task(task)
        self.local.concurrent_requests -= 1
        yield task
    
    def tick(self, now=None):
        '''Run a tick, that is one iteration of the scheduler. This
method only works when :attr:`schedule_periodic` is ``True`` and
the arbiter context.

Executes all due tasks and calculate the time in seconds to wait before
running a new :meth:`tick`. For testing purposes a :class:`datetime.datetime`
value ``now`` can be passed.'''
        if not self.schedule_periodic: 
            return
        remaining_times = []
        try:
            for entry in itervalues(self.entries):
                is_due, next_time_to_run = entry.is_due(now=now)
                if is_due:
                    self.run_job(entry.name)
                if next_time_to_run:
                    remaining_times.append(next_time_to_run)
        except Exception:
            LOGGER.exception('Unhandled error in task backend')
        self.next_run = now or datetime.now()
        if remaining_times:
            self.next_run += timedelta(seconds = min(remaining_times))
     
    ############################################################################
    ##    HOOKS
    ############################################################################
    def on_start_task(self, task):
        pass
    
    def on_timeout_task(self, task):
        pass
    
    def on_finish_task(self, task):
        pass
    
    ############################################################################
    ##    ABSTRACT METHODS
    ############################################################################
    def put_task(self, task_id):
        '''Put the *task_id* into the queue.'''
        raise NotImplementedError
    
    def get_task(self, task_id=None):
        '''retrieve a :class:`Task` from a task id. Must be implemented
by subclasses.'''
        raise NotImplementedError
    
    def save_task(self, task_id, **params):
        raise NotImplementedError
    
    def delete_tasks(self, task_ids=None):
        '''Delete a group of task'''
        raise NotImplementedError
    
    ############################################################################
    ##    PRIVATE METHODS
    ############################################################################

    def _setup_schedule(self):
        if not self.local.schedule_periodic:
            return
        entries = {}
        for name, task in self.registry.filter_types('periodic'):
            schedule = self._maybe_schedule(task.run_every, task.anchor)
            entries[name] = SchedulerEntry(name, schedule)
        return entries
    
    def _maybe_schedule(self, s, anchor):
        if not self.local.schedule_periodic:
            return
        if isinstance(s, int):
            s = timedelta(seconds=s)
        if not isinstance(s, timedelta):
            raise ValueError('Schedule %s is not a timedelta' % s)
        return Schedule(s, anchor)
    
    @async(get_result=False)
    def _run_job(self, jobname, targs, tkwargs, meta_params):
        task = yield self.create_task(jobname, targs, tkwargs, **meta_params)
        if task:
            yield self.queue_task(task)
    
class Schedule(object):

    def __init__(self, run_every=None, anchor=None):
        self.run_every = run_every
        self.anchor = anchor

    def remaining_estimate(self, last_run_at, now=None):
        """Returns when the periodic task should run next as a timedelta."""
        return remaining(last_run_at, self.run_every, now=now)

    def is_due(self, last_run_at, now=None):
        """Returns tuple of two items ``(is_due, next_time_to_run)``,
        where next time to run is in seconds.

        See :meth:`unuk.contrib.tasks.models.PeriodicTask.is_due` for more information.
        """
        rem_delta = self.remaining_estimate(last_run_at, now = now)
        rem = timedelta_seconds(rem_delta)
        if rem == 0:
            return True, timedelta_seconds(self.run_every)
        return False, rem


class SchedulerEntry(object):
    """A class used as a schedule entry in by a :class:`Scheduler`."""
    name = None
    '''Task name'''
    schedule = None
    '''The schedule'''
    last_run_at = None
    '''The time and date of when this task was last run.'''
    total_run_count = None
    '''Total number of times this periodic task has been executed by the
    :class:`Scheduler`.'''

    def __init__(self, name, schedule, args=(), kwargs={},
                 last_run_at = None, total_run_count=None):
        self.name = name
        self.schedule = schedule
        self.last_run_at = last_run_at or datetime.now()
        self.total_run_count = total_run_count or 0

    def __repr__(self):
        return self.name
    __str__ = __repr__

    @property
    def scheduled_last_run_at(self):
        '''The scheduled last run datetime. This is different from
:attr:`last_run_at` only when :attr:`anchor` is set.'''
        last_run_at = self.last_run_at
        anchor = self.anchor
        if last_run_at and anchor:
            run_every = self.run_every
            times = int(timedelta_seconds(last_run_at - anchor)\
                            /timedelta_seconds(run_every))
            if times:
                anchor += times*run_every
                while anchor <= last_run_at:
                    anchor += run_every
                while anchor > last_run_at:
                    anchor -= run_every
                self.schedule.anchor = anchor
            return anchor
        else:
            return last_run_at

    @property
    def run_every(self):
        return self.schedule.run_every

    @property
    def anchor(self):
        return self.schedule.anchor

    def next(self, now = None):
        """Returns a new instance of the same class, but with
        its date and count fields updated. Function called by :class:`Scheduler`
        when the ``this`` is due to run."""
        now = now or datetime.now()
        self.last_run_at = now or datetime.now()
        self.total_run_count += 1
        return self

    def is_due(self, now = None):
        return self.schedule.is_due(self.scheduled_last_run_at, now=now)