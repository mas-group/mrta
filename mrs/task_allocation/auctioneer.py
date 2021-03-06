import logging
from datetime import datetime
from datetime import timedelta

from mrs.db.models.task import TaskLot
from mrs.exceptions.task_allocation import AlternativeTimeSlot
from mrs.exceptions.task_allocation import NoAllocation
from mrs.structs.allocation import TaskAnnouncement, Allocation
from mrs.structs.timetable import Timetable
from mrs.task_allocation.round import Round
from ropod.structs.task import TaskStatus as TaskStatusConst
from ropod.utils.timestamp import TimeStamp
from stn.stp import STP

""" Implements a variation of the the TeSSI algorithm using the bidding_rule 
specified in the config file
"""


class Auctioneer(object):

    def __init__(self, ccu_store, api, stp_solver, allocation_method,
                 round_time=5, **kwargs):

        self.logger = logging.getLogger("mrs.auctioneer")

        self.robot_ids = list()
        self.timetables = dict()

        self.api = api
        self.stp = STP(stp_solver)

        self.allocation_method = allocation_method
        self.round_time = timedelta(seconds=round_time)
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)

        self.logger.debug("Auctioneer started")

        self.tasks_to_allocate = dict()
        self.allocations = list()
        self.waiting_for_user_confirmation = list()
        self.round = Round()

        # TODO: Update zero_timepoint
        today_midnight = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
        self.zero_timepoint = TimeStamp()
        self.zero_timepoint.timestamp = today_midnight

    def register_robot(self, robot_id):
        self.robot_ids.append(robot_id)
        self.get_timetable(robot_id)

    def get_timetable(self, robot_id):
        timetable = Timetable.fetch(robot_id, self.stp)
        self.timetables[robot_id] = timetable

    def run(self):
        if self.tasks_to_allocate and self.round.finished:
            self.announce_task()

        if self.round.opened and self.round.time_to_close():
            try:
                round_result = self.round.get_result()
                allocation = self.process_allocation(round_result)
                allocated_task, winner_robot_ids = allocation
                for robot_id in winner_robot_ids:
                    self.announce_winner(allocated_task, robot_id)

            except NoAllocation as exception:
                self.logger.error("No mrs made in round %s ", exception.round_id)
                self.round.finish()

            except AlternativeTimeSlot as exception:
                self.process_alternative_allocation(exception)
                self.round.finish()

    def process_allocation(self, round_result):

        task_lot, robot_id, position, tasks_to_allocate = round_result

        allocation = (task_lot.task.task_id, [robot_id])
        self.allocations.append(allocation)
        self.tasks_to_allocate = tasks_to_allocate

        self.logger.debug("Allocation: %s", allocation)
        self.logger.debug("Tasks to allocate %s", [task_id for task_id, task in self.tasks_to_allocate.items()])

        self.logger.debug("Updating task status to ALLOCATED")
        task_lot.task.update_status(TaskStatusConst.ALLOCATED)
        self.update_timetable(robot_id, task_lot, position)

        return allocation

    def update_timetable(self, robot_id, task_lot, position):
        self.get_timetable(robot_id)
        timetable = self.timetables.get(robot_id)
        timetable.zero_timepoint = self.zero_timepoint
        timetable.add_task_to_stn(task_lot, position)
        timetable.solve_stp()

        # Update schedule to reflect the changes in the dispatchable graph
        if timetable.schedule:
            # TODO: Request re-scheduling to the scheduler via pyre
            pass

        self.timetables.update({robot_id: timetable})
        timetable.store()

        self.logger.debug("STN robot %s: %s", robot_id, timetable.stn)
        self.logger.debug("Dispatchable graph robot %s: %s", robot_id, timetable.dispatchable_graph)

    def process_alternative_allocation(self, exception):
        task_id = exception.task_id
        robot_id = exception.robot_id
        alternative_start_time = exception.alternative_start_time
        self.logger.debug("Alternative timeslot for task %s: robot %s, alternative start time: %s ", task_id, robot_id,
                          alternative_start_time)

        alternative_allocation = (task_id, [robot_id], alternative_start_time)
        self.waiting_for_user_confirmation.append(alternative_allocation)

    def add_task(self, task):
        task_lot = TaskLot.from_task(task)
        self.tasks_to_allocate[task_lot.task.task_id] = task_lot

    def allocate(self, tasks):
        if isinstance(tasks, list):
            for task in tasks:
                self.add_task(task)
            self.logger.debug('Auctioneer received a list of tasks')
        else:
            self.add_task(tasks)
            self.logger.debug('Auctioneer received one task')

    def announce_task(self):

        round_ = {'tasks_to_allocate': self.tasks_to_allocate,
                  'round_time': self.round_time,
                  'n_robots': len(self.robot_ids),
                  'alternative_timeslots': self.alternative_timeslots}

        self.round = Round(**round_)

        self.logger.debug("Starting round: %s", self.round.id)
        self.logger.debug("Number of tasks to allocate: %s", len(self.tasks_to_allocate))

        tasks_lots = list(self.tasks_to_allocate.values())

        task_announcement = TaskAnnouncement(tasks_lots, self.round.id, self.zero_timepoint)
        msg = self.api.create_message(task_announcement)

        self.logger.debug("Auctioneer announces tasks %s", [task_id for task_id, task in self.tasks_to_allocate.items()])

        self.round.start()
        self.api.publish(msg, groups=['TASK-ALLOCATION'])

    def bid_cb(self, msg):
        payload = msg['payload']
        self.round.process_bid(payload)

    def finish_round_cb(self, msg):
        self.round.finish()

    def announce_winner(self, task_id, robot_id):
        allocation = Allocation(task_id, robot_id)
        msg = self.api.create_message(allocation)
        self.api.publish(msg, groups=['TASK-ALLOCATION'])

    def get_task_schedule(self, task_id, robot_id):
        # For now, returning the start navigation time from the dispatchable graph
        task_schedule = dict()

        timetable = self.timetables.get(robot_id)

        relative_start_navigation_time = timetable.dispatchable_graph.get_time(task_id, "navigation")
        relative_start_time = timetable.dispatchable_graph.get_time(task_id, "start")
        relative_latest_finish_time = timetable.dispatchable_graph.get_time(task_id, "finish", False)

        self.logger.debug("Current time %s: ", TimeStamp())
        self.logger.debug("zero_timepoint %s: ", self.zero_timepoint)
        self.logger.debug("Relative start navigation time: %s", relative_start_navigation_time)
        self.logger.debug("Relative start time: %s", relative_start_time)
        self.logger.debug("Relative latest finish time: %s", relative_latest_finish_time)

        start_navigation_time = self.zero_timepoint + timedelta(minutes=relative_start_navigation_time)
        start_time = self.zero_timepoint + timedelta(minutes=relative_start_time)
        finish_time = self.zero_timepoint + timedelta(minutes=relative_latest_finish_time)

        self.logger.debug("Start navigation of task %s: %s", task_id, start_navigation_time)
        self.logger.debug("Start of task %s: %s", task_id, start_time)
        self.logger.debug("Latest finish of task %s: %s", task_id, finish_time)

        task_schedule['start_time'] = start_navigation_time.to_datetime()
        task_schedule['finish_time'] = finish_time.to_datetime()

        return task_schedule


class AuctioneerBuilder:
    def __init__(self):
        self._instance = None

    def __call__(self, **kwargs):
        if not self._instance:
            self._instance = Auctioneer(**kwargs)
        return self._instance


configure = AuctioneerBuilder()
