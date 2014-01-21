from __future__ import absolute_import

import logging

from datetime import datetime, timedelta
from threading import local, Lock

from changes.config import db, queue
from changes.constants import Result, Status
from changes.db.utils import try_create, get_or_create
from changes.models import Task
from changes.utils.locking import lock


RETRY_COUNTDOWN = 60
CONTINUE_COUNTDOWN = 5

RUN_TIMEOUT = timedelta(minutes=5)
EXPIRE_TIMEOUT = timedelta(minutes=30)


def needs_requeued(task):
    current_datetime = datetime.utcnow()
    run_datetime = current_datetime - RUN_TIMEOUT
    return task.date_modified < run_datetime


class NotFinished(Exception):
    pass


class TrackedTask(local):
    """
    Tracks the state of the given Task and it's children.

    Tracked tasks **never** return a result.

    >>> @tracked_task
    >>> def func(foo):
    >>>    if random.randint(0, 1) == 1:
    >>>        # re-queue for further results
    >>>        raise func.NotFinished
    >>>
    >>>    elif random.randint(0, 1) == 1:
    >>>        # cause an exception to retry
    >>>        raise Exception
    >>>
    >>>    # finish normally to update Status
    >>>    print "func", foo

    >>> foo.delay(foo='bar', task_id='bar')
    """
    NotFinished = NotFinished

    def __init__(self, func):
        self.func = lock(func)
        self.task_name = func.__name__
        self.parent_id = None
        self.task_id = None
        self.lock = Lock()
        self.logger = logging.getLogger('jobs.{0}'.format(self.task_name))

        self.__name__ = func.__name__
        self.__doc__ = func.__doc__
        self.__wraps__ = getattr(func, '__wraps__', func)

    def __call__(self, **kwargs):
        with self.lock:
            self._run(kwargs)

    def _run(self, kwargs):
        self.task_id = kwargs.pop('task_id', None)
        if not self.task_id:
            self.logger.warning('Missing task_id for job: %r', kwargs)
            self.func(**kwargs)

        self.parent_id = kwargs.pop('parent_task_id', None)
        self.kwargs = kwargs

        date_started = datetime.utcnow()

        try:
            self.func(**kwargs)

        except NotFinished:
            self.logger.debug(
                'Task marked as not finished: %s %s', self.task_name, self.task_id)
            kwargs['task_id'] = self.task_id
            kwargs['parent_task_id'] = self.parent_id

            self._update({
                Task.date_modified: datetime.utcnow(),
                Task.status: Status.in_progress,
            })

            db.session.commit()

            queue.delay(
                self.task_name,
                kwargs=kwargs,
                countdown=CONTINUE_COUNTDOWN,
            )

        except Exception as exc:
            db.session.rollback()

            self.logger.exception(unicode(exc))

            try:
                self._retry()
            except Exception as exc:
                self.logger.exception(unicode(exc))
                raise

        else:
            date_finished = datetime.utcnow()

            try:
                self._update({
                    Task.date_started: date_started,
                    Task.date_finished: date_finished,
                    Task.date_modified: date_finished,
                    Task.status: Status.finished,
                })
            except Exception as exc:
                self.logger.exception(unicode(exc))
                raise

            db.session.commit()
        finally:
            db.session.expire_all()

            self.task_id = None
            self.parent_id = None
            self.kwargs = kwargs

    def _update(self, kwargs):
        """
        Update's the state of this Task.

        >>> task._update(status=Status.finished)
        """
        assert self.task_id

        Task.query.filter(
            Task.task_name == self.task_name,
            Task.parent_id == self.parent_id,
            Task.task_id == self.task_id,
        ).update(kwargs, synchronize_session=False)

    def _retry(self):
        """
        Retry this task and update it's state.

        >>> task.retry()
        """
        # TODO(dcramer): this needs to handle too-many-retries itself
        assert self.task_id

        self._update({
            Task.date_modified: datetime.utcnow(),
            Task.status: Status.in_progress,
            Task.num_retries: Task.num_retries + 1,
        })

        kwargs = self.kwargs.copy()
        kwargs['task_id'] = self.task_id
        kwargs['parent_task_id'] = self.parent_id
        queue.delay(
            self.task_name,
            kwargs=kwargs,
            countdown=RETRY_COUNTDOWN,
        )

    def delay_if_needed(self, **kwargs):
        """
        Enqueue this task if it's new or hasn't checked in in a reasonable
        amount of time.

        >>> task.delay_if_needed(
        >>>     task_id='33846695b2774b29a71795a009e8168a',
        >>>     parent_task_id='659974858dcf4aa08e73a940e1066328',
        >>> )
        """
        assert kwargs.get('task_id')

        task, created = get_or_create(Task, where={
            'task_name': self.task_name,
            'parent_id': kwargs.get('parent_task_id'),
            'task_id': kwargs['task_id'],
        }, defaults={
            'status': Status.queued,
        })

        if created or needs_requeued(task):
            queue.delay(
                self.task_name,
                kwargs=kwargs,
                countdown=CONTINUE_COUNTDOWN,
            )

    def delay(self, **kwargs):
        """
        Enqueue this task.

        >>> task.delay(
        >>>     task_id='33846695b2774b29a71795a009e8168a',
        >>>     parent_task_id='659974858dcf4aa08e73a940e1066328',
        >>> )
        """
        assert kwargs.get('task_id')

        try_create(Task, where={
            'task_name': self.task_name,
            'parent_id': kwargs.get('parent_task_id'),
            'task_id': kwargs['task_id'],
            'status': Status.queued,
        })

        queue.delay(
            self.task_name,
            kwargs=kwargs,
            countdown=CONTINUE_COUNTDOWN,
        )

    def verify_all_children(self):
        task_list = list(Task.query.filter(
            Task.parent_id == self.task_id
        ))

        current_datetime = datetime.utcnow()
        expire_datetime = current_datetime - EXPIRE_TIMEOUT

        need_expire = set()
        need_run = set()

        has_pending = False

        for task in task_list:
            if task.status == Status.finished:
                continue

            if task.date_modified < expire_datetime:
                need_expire.add(task.task_id.hex)
                continue

            has_pending = True

            if needs_requeued(task):
                need_run.add(task.task_id.hex)

        if need_expire:
            Task.query.filter(
                Task.task_name == task.task_name,
                Task.parent_id == self.task_id,
                Task.task_id.in_([n for n in need_expire]),
            ).update({
                Task.date_modified: current_datetime,
                Task.status: Status.finished,
                Task.result: Result.aborted,
            }, synchronize_session=False)
            db.session.commit()

        # TODO(dcramer): if we store params with Task we could re-run
        # failed tasks here
        # if need_run:
        #     Task.query.filter(
        #         Task.task_name == task.task_name,
        #         Task.parent_id == self.task_id,
        #         Task.task_id.in_([n for n in need_run]),
        #     ).update({
        #         Task.date_modified: current_datetime,
        #     }, synchronize_session=False)

        if has_pending:
            status = Status.in_progress

        else:
            status = Status.finished

        return status

    def verify_children(self, task_name, child_ids=(), kwarg_func=lambda x: {},
                        create=True):
        """
        Ensure all child tasks are running. If child_ids is
        present this will automatically manage creation of the any missing jobs.

        Return the aggregate status of child tasks.

        >>> child_status = task.ensure_all([1, 2], params=lambda child_id: {
        >>>     'job_id': child_id
        >>> })
        """
        # TODO(dcramer): once we've migrated legacy tasks we should remove the
        # "auto create" jobs and have this simply do a check
        assert self.task_id

        if not child_ids:
            return Status.finished

        current_datetime = datetime.utcnow()
        expire_datetime = current_datetime - EXPIRE_TIMEOUT

        task_list = list(Task.query.filter(
            Task.task_name == task_name,
            Task.parent_id == self.task_id,
        ))

        if create:
            need_created = set(child_ids)
        else:
            need_created = frozenset()
        need_expire = set()
        need_run = set()
        has_pending = False

        for task in task_list:
            try:
                need_created.remove(task.task_id.hex)
            except KeyError:
                pass

            if task.status == Status.finished:
                continue

            if task.date_modified < expire_datetime:
                need_expire.add(task.task_id.hex)
                continue

            has_pending = True

            if needs_requeued(task):
                need_run.add(task.task_id.hex)

        if need_expire:
            Task.query.filter(
                Task.task_name == task.task_name,
                Task.parent_id == self.task_id,
                Task.task_id.in_([n for n in need_expire]),
            ).update({
                Task.date_modified: current_datetime,
                Task.status: Status.finished,
                Task.result: Result.aborted,
            }, synchronize_session=False)

        if need_run:
            Task.query.filter(
                Task.task_name == task.task_name,
                Task.parent_id == self.task_id,
                Task.task_id.in_([n for n in need_run]),
            ).update({
                Task.date_modified: current_datetime,
            }, synchronize_session=False)

        for child_id in need_created:
            child_task = try_create(Task, where={
                'task_name': task_name,
                'parent_id': self.task_id,
                'task_id': child_id,
            })
            if child_task:
                need_run.add(child_id)

        db.session.commit()

        for child_id in need_run:
            child_kwargs = kwarg_func(child_id)
            child_kwargs['parent_task_id'] = self.task_id
            child_kwargs['task_id'] = child_id
            queue.delay(task_name, kwargs=child_kwargs)

        if has_pending or need_created or need_run:
            status = Status.in_progress

        else:
            status = Status.finished

        return status


# bind to a decorator-like naming scheme
tracked_task = TrackedTask