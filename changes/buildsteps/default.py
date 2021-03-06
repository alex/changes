from __future__ import absolute_import

from changes.buildsteps.base import BuildStep
from changes.config import db
from changes.constants import Status
from changes.db.utils import get_or_create
from changes.jobs.sync_job_step import sync_job_step
from changes.models import Command as CommandModel, JobPhase, JobStep


class Command(object):
    def __init__(self, script, path='', artifacts=None, env=None):
        self.script = script
        self.path = path
        self.artifacts = artifacts or ()
        self.env = env or {}


class DefaultBuildStep(BuildStep):
    """
    A build step which relies on the a scheduling framework in addition to the
    Changes client (or some other push source).

    Jobs will get allocated via a polling step that is handled by the external
    scheduling framework. Once allocated a job is expected to begin reporting
    within a given timeout. All results are expected to be pushed via APIs.
    """
    def __init__(self, commands, path='', env=None, artifacts=None, **kwargs):
        command_defaults = (
            ('path', path),
            ('env', env),
            ('artifacts', artifacts),
        )
        for command in commands:
            for k, v in command_defaults:
                if k not in command:
                    command[k] = v

        self.commands = map(lambda x: Command(**x), commands)

        super(DefaultBuildStep, self).__init__(**kwargs)

    def get_label(self):
        return 'Build via Changes Client'

    def execute(self, job):
        job.status = Status.queued
        db.session.add(job)

        phase, created = get_or_create(JobPhase, where={
            'job': job,
            'label': job.label,
        }, defaults={
            'status': Status.queued,
            'project': job.project,
        })

        step, created = get_or_create(JobStep, where={
            'phase': phase,
            'label': job.label,
        }, defaults={
            'status': Status.pending_allocation,
            'job': phase.job,
            'project': phase.project,
        })

        for index, command in enumerate(self.commands):
            command_model, created = get_or_create(CommandModel, where={
                'jobstep': step,
                'order': index,
            }, defaults={
                'label': command.script.splitlines()[0][:128],
                'status': Status.queued,
                'script': command.script,
                'env': command.env,
                'cwd': command.path,
                'artifacts': command.artifacts,
            })
        db.session.commit()

        sync_job_step.delay(
            step_id=step.id.hex,
            task_id=step.id.hex,
            parent_task_id=job.id.hex,
        )

    def update(self, job):
        pass

    def update_step(self, step):
        """
        Look for allocated JobStep's and re-queue them if elapsed time is
        greater than allocation timeout.
        """
        # TODO(cramer):

    def cancel_step(self, step):
        pass
