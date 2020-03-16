import logging
from datetime import datetime, timedelta

import dateutil.parser
import numpy as np
from fmlib.models.requests import TransportationRequest
from fmlib.models.tasks import Task as BaseTask
from fmlib.models.tasks import TaskManager
from fmlib.utils.messages import Document
from pymodm import EmbeddedMongoModel, fields
from pymodm.context_managers import switch_collection
from pymodm.context_managers import switch_connection
from pymongo.errors import ServerSelectionTimeoutError
from ropod.utils.timestamp import TimeStamp


class TimepointConstraint(EmbeddedMongoModel):
    name = fields.CharField(primary_key=True)
    earliest_time = fields.DateTimeField()
    latest_time = fields.DateTimeField()

    def __str__(self):
        to_print = ""
        to_print += "{}: [{}, {}]".format(self.name, self.earliest_time.isoformat(), self.latest_time.isoformat())
        return to_print

    @classmethod
    def from_payload(cls, payload):
        document = Document.from_payload(payload)
        document["_id"] = document.pop("name")
        document["earliest_time"] = dateutil.parser.parse(document.pop("earliest_time"))
        document["latest_time"] = dateutil.parser.parse(document.pop("latest_time"))
        return cls.from_document(document)

    def to_dict(self):
        dict_repr = dict()
        dict_repr["name"] = self.name
        dict_repr["earliest_time"] = self.earliest_time.isoformat()
        dict_repr["latest_time"] = self.latest_time.isoformat()
        return dict_repr

    def relative_to_ztp(self, ztp):
        if self.earliest_time.isoformat().startswith("9999-12-31T"):
            r_earliest_time = np.inf
        else:
            r_earliest_time = TimeStamp.from_datetime(self.earliest_time).get_difference(ztp).total_seconds()
        if self.latest_time.isoformat().startswith("9999-12-31T"):
            r_latest_time = np.inf
        else:
            r_latest_time = TimeStamp.from_datetime(self.latest_time).get_difference(ztp).total_seconds()

        return r_earliest_time, r_latest_time

    @staticmethod
    def absolute_time(ztp, r_time):
        if r_time == np.inf:
            return datetime.max
        time_ = ztp + timedelta(seconds=r_time)
        return time_.to_datetime()

    def to_dict_relative_to_ztp(self, ztp):
        r_earliest_time, r_latest_time = self.relative_to_ztp(ztp)
        dict_repr = dict()
        dict_repr["name"] = self.name
        dict_repr["r_earliest_time"] = r_earliest_time
        dict_repr["r_latest_time"] = r_latest_time
        return dict_repr


class InterTimepointConstraint(EmbeddedMongoModel):
    name = fields.CharField()
    mean = fields.FloatField()
    variance = fields.FloatField()

    @property
    def standard_dev(self):
        return round(self.variance ** 0.5, 3)

    def __str__(self):
        to_print = ""
        to_print += "{}: N({}, {})".format(self.name, self.mean, self.variance ** 0.5)
        return to_print

    @classmethod
    def from_payload(cls, payload):
        document = Document.from_payload(payload)
        return cls.from_document(document)

    def to_dict(self):
        dict_repr = self.to_son().to_dict()
        dict_repr.pop('_cls')
        return dict_repr


class TemporalConstraints(EmbeddedMongoModel):
    hard = fields.BooleanField(default=True)
    timepoint_constraints = fields.EmbeddedDocumentListField(TimepointConstraint)
    inter_timepoint_constraints = fields.EmbeddedDocumentListField(InterTimepointConstraint)

    @classmethod
    def from_payload(cls, payload):
        document = Document.from_payload(payload)
        timepoint_constraints = [TimepointConstraint.from_payload(timepoint_constraint)
                                 for timepoint_constraint in document.get("timepoint_constraints")]
        inter_timepoint_constraints = [InterTimepointConstraint.from_payload(inter_timepoint_constraint)
                                       for inter_timepoint_constraint in document.get("inter_timepoint_constraints")]
        document["timepoint_constraints"] = timepoint_constraints
        document["inter_timepoint_constraints"] = inter_timepoint_constraints
        temporal_constraints = TemporalConstraints.from_document(document)
        return temporal_constraints

    def to_dict(self):
        dict_repr = self.to_son().to_dict()
        dict_repr.pop('_cls')
        timepoint_constraints = [timepoint_constraint.to_dict() for timepoint_constraint in self.timepoint_constraints]
        inter_timepoint_constraints = [inter_timepoint_constraint.to_dict() for inter_timepoint_constraint in self.inter_timepoint_constraints]
        dict_repr["timepoint_constraints"] = timepoint_constraints
        dict_repr["inter_timepoint_constraints"] = inter_timepoint_constraints
        return dict_repr


class Task(BaseTask):
    constraints = fields.EmbeddedDocumentField(TemporalConstraints)
    frozen = fields.BooleanField(default=False)

    objects = TaskManager()

    def set_soft_constraints(self):
        self.constraints.hard = False
        self.save()

    def freeze(self):
        self.frozen = True
        self.save()

    def unfreeze(self):
        self.frozen = False
        self.save()

    def mark_as_delayed(self):
        task_status = Task.get_task_status(self.task_id)
        task_status.delayed = True
        task_status.save()

    def unmark_as_delayed(self):
        task_status = Task.get_task_status(self.task_id)
        task_status.delayed = False
        task_status.save()

    def update_start_time(self, start_time):
        self.start_time = start_time
        self.save()

    def update_finish_time(self, finish_time):
        self.finish_time = finish_time
        self.save()

    def get_timepoint_constraint(self, name):
        for constraint in self.constraints.timepoint_constraints:
            if constraint.name == name:
                return constraint

    def get_inter_timepoint_constraint(self, name):
        for constraint in self.constraints.inter_timepoint_constraints:
            if constraint.name == name:
                return constraint

    def get_timepoint_constraints(self):
        return self.constraints.timepoint_constraints

    def get_inter_timepoint_constraints(self):
        return self.constraints.inter_timepoint_constraints

    def update_timepoint_constraint(self, name, earliest_time, latest_time=np.inf):
        in_list = False
        for constraint in self.constraints.timepoint_constraints:
            if constraint.name == name:
                in_list = True
                constraint.earliest_time = earliest_time
                constraint.latest_time = latest_time
        if not in_list:
            self.constraints.timepoint_constraints.append(TimepointConstraint(name=name,
                                                                              earliest_time=earliest_time,
                                                                              latest_time=latest_time))
        self.save()

    def update_inter_timepoint_constraint(self, name, mean, variance):
        in_list = False
        for constraint in self.constraints.inter_timepoint_constraints:
            if constraint.name == name:
                in_list = True
                constraint.mean = mean
                constraint.variance = variance
        if not in_list:
            self.constraints.inter_timepoint_constraints.append(InterTimepointConstraint(name=name,
                                                                                         mean=mean,
                                                                                         variance=variance))
        self.save()

    @staticmethod
    def get_earliest_task(tasks):
        earliest_time = datetime.max
        earliest_task = None
        for task in tasks:
            timepoint_constraints = task.get_timepoint_constraints()
            for constraint in timepoint_constraints:
                if constraint.earliest_time < earliest_time:
                    earliest_time = constraint.earliest_time
                    earliest_task = task
        return earliest_task

    def remove(self):
        self.status.archive()
        self.archive()

    @classmethod
    def from_task(cls, task):
        # TODO: Receive mean and variance work time
        pickup_constraint = TimepointConstraint(name="pickup",
                                                earliest_time=task.request.earliest_pickup_time,
                                                latest_time=task.request.latest_pickup_time)

        travel_time = InterTimepointConstraint(name="travel_time")

        work_time = InterTimepointConstraint(name="work_time",
                                             mean=(task.request.latest_pickup_time - task.request.earliest_pickup_time).total_seconds(),
                                             variance=0.1)

        constraints = TemporalConstraints(timepoint_constraints=[pickup_constraint],
                                          inter_timepoint_constraints=[travel_time, work_time],
                                          hard=task.request.hard_constraints)

        return cls.create_new(task_id=task.task_id,
                              request=task.request,
                              constraints=constraints)

    @classmethod
    def from_payload(cls, payload):
        document = Document.from_payload(payload)
        document['_id'] = document.pop('task_id')
        document["constraints"] = TemporalConstraints.from_payload(document.pop("constraints"))
        document["request"] = TransportationRequest.from_payload(document.pop("request"))
        task = cls.from_document(document)
        task.save()
        return task

    def to_dict(self):
        dict_repr = super().to_dict()
        dict_repr["constraints"] = self.constraints.to_dict()
        dict_repr["request"] = self.request.to_dict()
        return dict_repr

    @classmethod
    def get_task(cls, task_id):
        return cls.objects.get_task(task_id)

    @classmethod
    def get_tasks_by_robot(cls, robot_id):
        return [task for task in cls.objects.all() if robot_id in task.assigned_robots]

    def archive(self):
        try:
            with switch_connection(Task, "default"):
                with switch_collection(Task, Task.Meta.archive_collection):
                    super().save()
                self.delete()

        except ServerSelectionTimeoutError:
            logging.warning('Could not save models to MongoDB')
