import logging
import time

from fleet_management.api import API
from fleet_management.config.loader import Configurator
from fleet_management.db.mongo import Store
from mrs.resource_manager import ResourceManager
from mrs import config

_component_modules = {'api': API,
                      'ccu_store': Store,
                      'resource_manager': ResourceManager,
                      }

_config_order = ['api', 'ccu_store', 'resource_manager']

_plugin_builders = {'mrta': config.configure}


class FMS(object):
    def __init__(self, config_file=None):
        self.logger = logging.getLogger('mrs')

        configurator = Configurator(config_file,
                                    component_modules=_component_modules,
                                    config_order=_config_order,
                                    plugin_builders=_plugin_builders)
        configurator.configure()
        self.resource_manager = configurator.resource_manager

        self.api = configurator.api
        self.api.register_callbacks(self)
        self.logger.info("Initialized MRS")

    def run(self):
        try:
            self.api.start()

            while True:
                self.resource_manager.auctioneer.run()
                self.resource_manager._get_allocation()
                self.api.run()
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            self.api.shutdown()
            self.logger.info('FMS is shutting down')

    def shutdown(self):
        self.api.shutdown()


if __name__ == '__main__':
    config_file = '../config/config.yaml'
    fms = FMS(config_file)

    fms.run()
