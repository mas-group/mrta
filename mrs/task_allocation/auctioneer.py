import uuid
import time
import logging
import logging.config
from datetime import timedelta
from mrs.task_allocation.round import Round
from mrs.timetable import Timetable
from stn.stp import STP
from mrs.exceptions.task_allocation import NoAllocation
from mrs.exceptions.task_allocation import AlternativeTimeSlot


""" Implements a variation of the the TeSSI algorithm using the bidding_rule 
specified in the config file
"""


class Auctioneer(object):

    def __init__(self, robot_ids, ccu_store, api, stp_solver, task_cls, allocation_method, round_time=5,
                 **kwargs):

        logging.debug("Starting Auctioneer")

        self.robot_ids = robot_ids
        self.ccu_store = ccu_store
        self.api = api
        self.allocation_method = allocation_method
        self.round_time = timedelta(seconds=round_time)
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)

        self.stp = STP(stp_solver)
        self.task_cls = task_cls

        # TODO: Inititalize the timetables in the loader? and read the timetables here
        self.timetables = dict()
        for robot_id in robot_ids:
            timetable = Timetable(self.stp, robot_id)
            self.timetables[robot_id] = timetable
            self.ccu_store.add_timetable(timetable)

        self.tasks_to_allocate = dict()
        self.allocations = list()
        self.waiting_for_user_confirmation = list()
        self.round = Round()

        self.register_api_callbacks()

    def __str__(self):
        to_print = "Auctioneer"
        to_print += '\n'
        to_print += "Groups {}".format(self.api.interfaces[0].groups())
        return to_print

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
                logging.exception("No mrs made in round %s ", exception.round_id)
                self.round.finish()

            except AlternativeTimeSlot as exception:
                self.process_alternative_allocation(exception)
                self.round.finish()

    def process_allocation(self, round_result):

        task, robot_id, position, tasks_to_allocate = round_result

        allocation = (task.id, [robot_id])
        self.allocations.append(allocation)
        self.tasks_to_allocate = tasks_to_allocate

        logging.debug("Allocation: %s", allocation)
        logging.debug("Tasks to allocate %s", self.tasks_to_allocate)

        self.update_task_status(task, 2)  # 2 is ALLOCATED
        self.update_timetable(robot_id, task, position)

        return allocation

    def update_task_status(self, task, status):
        task.status.status = status
        logging.debug("Updating task status to %s", task.status.status)
        self.ccu_store.update_task(task)

    def update_timetable(self, robot_id, task, position):
        timetable = Timetable.get_timetable(self.ccu_store, robot_id, self.stp)
        timetable.add_task_to_stn(task, position)
        timetable.solve_stp()

        # Update schedule to reflect the changes in the dispatchable graph
        if timetable.is_scheduled():
            # TODO: Request re-scheduling to the scheduler via pyre
            pass

        self.timetables.update({robot_id: timetable})
        self.ccu_store.update_timetable(timetable)

        logging.debug("STN robot %s: %s", robot_id, timetable.stn)
        logging.debug("Dispatchable graph robot %s: %s", robot_id, timetable.dispatchable_graph)

    def process_alternative_allocation(self, exception):
        task_id = exception.task_id
        robot_id = exception.robot_id
        alternative_start_time = exception.alternative_start_time
        logging.exception("Alternative timeslot for task %s: robot %s, alternative start time: %s ", task_id, robot_id,
                          alternative_start_time)

        alternative_allocation = (task_id, [robot_id], alternative_start_time)
        self.waiting_for_user_confirmation.append(alternative_allocation)

    def add_task(self, task):
        self.tasks_to_allocate[task.id] = task
        self.ccu_store.add_task(task)

    def allocate(self, tasks):
        if isinstance(tasks, list):
            for task in tasks:
                self.add_task(task)
            logging.debug('Auctioneer received a list of tasks')
        else:
            self.add_task(tasks)
            logging.debug('Auctioneer received one task')

    def announce_task(self):

        round_ = {'tasks_to_allocate': self.tasks_to_allocate,
                  'round_time': self.round_time,
                  'n_robots': len(self.robot_ids),
                  'alternative_timeslots': self.alternative_timeslots}

        self.round = Round(**round_)

        logging.info("Starting round: %s", self.round.id)
        logging.info("Number of tasks to allocate: %s", len(self.tasks_to_allocate))

        # Create task announcement message that contains all unallocated tasks
        task_announcement = dict()
        task_announcement['header'] = dict()
        task_announcement['payload'] = dict()
        task_announcement['header']['type'] = 'TASK-ANNOUNCEMENT'
        task_announcement['header']['metamodel'] = 'ropod-msg-schema.json'
        task_announcement['header']['msgId'] = str(uuid.uuid4())
        task_announcement['header']['timestamp'] = int(round(time.time()) * 1000)
        task_announcement['payload']['metamodel'] = 'ropod-task-announcement-schema.json'
        task_announcement['payload']['round_id'] = self.round.id
        task_announcement['payload']['tasks'] = dict()

        for task_id, task in self.tasks_to_allocate.items():
            task_announcement['payload']['tasks'][task.id] = task.to_dict()

        logging.debug("Auctioneer announces tasks %s", [task_id for task_id, task in self.tasks_to_allocate.items()])

        self.round.start()
        self.api.publish(task_announcement, groups=['TASK-ALLOCATION'])

    def task_cb(self, msg):
        task_dict = msg['payload']['task']
        task = self.task_cls.from_dict(task_dict)
        self.add_task(task)

    def bid_cb(self, msg):
        bid = msg['payload']['bid']
        self.round.process_bid(bid)

    def finish_round_cb(self, msg):
        self.round.finish()

    def announce_winner(self, task_id, robot_id):

        allocation = dict()
        allocation['header'] = dict()
        allocation['payload'] = dict()
        allocation['header']['type'] = 'ALLOCATION'
        allocation['header']['metamodel'] = 'ropod-msg-schema.json'
        allocation['header']['msgId'] = str(uuid.uuid4())
        allocation['header']['timestamp'] = int(round(time.time()) * 1000)

        allocation['payload']['metamodel'] = 'ropod-mrs-schema.json'
        allocation['payload']['task_id'] = task_id
        allocation['payload']['winner_id'] = robot_id

        logging.debug("Accouncing winner...")
        self.api.publish(allocation, groups=['TASK-ALLOCATION'])

    def get_task_schedule(self, task_id, robot_id):
        # For now, returning the start navigation time from the dispatchable graph

        task_schedule = dict()

        timetable = self.timetables.get(robot_id)

        start_time = timetable.dispatchable_graph.get_task_navigation_start_time(task_id)

        logging.debug("Start time of task %s: %s", task_id, start_time)

        task_schedule['start_time'] = start_time
        task_schedule['finish_time'] = -1  # This info is not available here.

        return task_schedule

    def register_api_callbacks(self):
        for option in self.api.middleware_collection:
            option_config = self.api.config_params.get(option, None)
            if option_config is None:
                continue

            callbacks = option_config.get('callbacks', list())
            for callback in callbacks:
                component = callback.pop('component', None)
                function = self.__get_callback_function(component)
                self.api.register_callback(option, function, **callback)

    def __get_callback_function(self, component):
        print("component: ", component)

        objects = component.split('.')
        function_name = objects.pop(1)
        print("function_name: ", function_name)
        function = getattr(self, function_name)
        return function


if __name__ == '__main__':

    from fleet_management.config.loader import Config
    config_file_path = '../../config/config.yaml'
    config = Config(config_file_path, initialize=True)
    auctioneer = config.configure_task_allocator(config.ccu_store)

    time.sleep(5)

    auctioneer.api.start()

    try:
        while True:
            auctioneer.api.run()
            auctioneer.run()
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        logging.info("Terminating %s auctioneer ...")
        auctioneer.api.shutdown()
        logging.info("Exiting...")
