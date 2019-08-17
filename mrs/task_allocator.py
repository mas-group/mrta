import logging
import time

from fleet_management.config.loader import Config, register_api_callbacks


class TaskAllocator(object):
    def __init__(self, config_file=None):
        self.logger = logging.getLogger('mrs')
        self.logger.info("Starting MRS...")
        self.config = Config(config_file, initialize=True)
        self.config.configure_logger()
        self.ccu_store = self.config.ccu_store
        self.api = self.config.api

        self.auctioneer = self.config.configure_task_allocator(self.ccu_store)
        self.task_monitor = self.config.configure_task_monitor(self.ccu_store)

        register_api_callbacks(self, self.api)

    def run(self):
        try:
            self.api.start()
            while True:
                self.api.run()
                self.auctioneer.run()
                self.task_monitor.run()
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            self.logger.info("Terminating %s auctioneer ...")
            self.api.shutdown()
            logging.info("Exiting...")


if __name__ == '__main__':
    config_file_path = '../config/config.yaml'
    task_allocator = TaskAllocator(config_file_path)
    task_allocator.run()