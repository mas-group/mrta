import copy
import logging

from ropod.utils.timestamp import TimeStamp
from ropod.utils.uuid import generate_uuid

from mrs.exceptions.task_allocation import AlternativeTimeSlot
from mrs.exceptions.task_allocation import NoAllocation
from mrs.structs.bid import Bid
import numpy as np


class Round(object):

    def __init__(self, **kwargs):

        self.logger = logging.getLogger('mrs.auctioneer.round')

        self.tasks_to_allocate = kwargs.get('tasks_to_allocate', dict())
        self.round_time = kwargs.get('round_time', 0)
        self.n_robots = kwargs.get('n_robots', 0)
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)

        self.closure_time = 0
        self.id = generate_uuid()
        self.finished = True
        self.opened = False
        self.received_bids = dict()
        self.received_no_bids = dict()

    def start(self):
        """ Starts and auction round:
        - opens the round
        - marks the round as not finished

        opened: The auctioneer processes bid msgs
        closed: The auctioneer no longer processes incoming bid msgs, i.e.,
                bid msgs received after the round has closed are not
                considered in the election process

        After the round closes, the election process takes place

        finished: The election process is over, i.e., an mrs has been made
                    (or an exception has been raised)

        """
        open_time = TimeStamp()
        self.closure_time = TimeStamp(delta=self.round_time)
        self.logger.debug("Round opened at %s and will close at %s",
                          open_time, self.closure_time)

        self.finished = False
        self.opened = True

    def process_bid(self, payload):
        bid = Bid.from_payload(payload)

        self.logger.debug("Processing bid from robot %s: (risk metric: %s, temporal metric: %s)",
                          bid.robot_id, bid.risk_metric, bid.temporal_metric)

        if bid.cost != (np.inf, np.inf):
            # Process a bid
            if bid.task_id not in self.received_bids or \
                    self.update_task_bid(bid, self.received_bids[bid.task_id]):

                self.received_bids[bid.task_id] = bid

        else:
            # Process a no-bid
            self.received_no_bids[bid.task_id] = self.received_no_bids.get(bid.task_id, 0) + 1

    @staticmethod
    def update_task_bid(new_bid, old_bid):
        """ Called when more than one bid is received for the same task

        :return: boolean
        """
        old_robot_id = int(old_bid.robot_id.split('_')[-1])
        new_robot_id = int(new_bid.robot_id.split('_')[-1])

        if new_bid < old_bid or (new_bid == old_bid and new_robot_id < old_robot_id):
            return True

        return False

    def time_to_close(self):
        current_time = TimeStamp()

        if current_time < self.closure_time:
            return False

        self.logger.debug("Closing round at %s", current_time)
        self.opened = False
        return True

    def get_result(self):
        """ Returns the results of the mrs as a tuple

        :return: round_result

        task, robot_id, position, tasks_to_allocate = round_result

        task (obj): task allocated in this round
        robot_id (string): id of the winning robot
        position (int): position in the STN where the task was added
        tasks_to_allocate (dict): tasks left to allocate

        """
        # Check for which tasks the constraints need to be set to soft
        if self.alternative_timeslots and self.received_no_bids:
            self.set_soft_constraints()

        try:
            winning_bid = self.elect_winner()
            allocated_task = self.tasks_to_allocate.pop(winning_bid.task_id, None)
            robot_id = winning_bid.robot_id
            position = winning_bid.position
            round_result = (allocated_task, robot_id, position, self.tasks_to_allocate)

            if winning_bid.hard_constraints is False:
                raise AlternativeTimeSlot(winning_bid.task_id, winning_bid.robot_id, winning_bid.alternative_start_time)

            return round_result

        except NoAllocation:
            self.logger.error("No mrs made in round %s ", self.id)
            raise NoAllocation(self.id)

    def finish(self):
        self.finished = True
        self.logger.debug("Round finished")

    def set_soft_constraints(self):
        """ If the number of no-bids for a task is equal to the number of robots,
        set the temporal constraints to soft
        """

        for task_id, n_no_bids in self.received_no_bids.items():
            if n_no_bids == self.n_robots:
                task = self.tasks_to_allocate.get(task_id)
                task.hard_constraints = False
                self.tasks_to_allocate.update({task_id: task})
                self.logger.debug("Setting soft constraints for task %s", task_id)

    def elect_winner(self):
        """ Elects the winner of the round

        :return:
        mrs(dict): key - task_id,
                          value - list of robots assigned to the task

        """
        lowest_bid = None

        for task_id, bid in self.received_bids.items():
            if lowest_bid is None or bid < lowest_bid:
                lowest_bid = copy.deepcopy(bid)

        if lowest_bid is None:
            raise NoAllocation(self.id)

        return lowest_bid

