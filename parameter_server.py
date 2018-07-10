"""
Parameter server for distbelief
"""
import torch
import torch.distributed as dist
from torch.multiprocessing import Process
from utils import ravel_model_params, send_message, init_processes, DEFAULT_LEARNING_RATE, MessageCode, MessageListener, unravel_model_params

from torchvision import datasets, transforms
from main import Net
from experimental import parameter_server_test, evaluate

import logging

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')

_LOGGER = logging.getLogger(__name__)

class ParameterServer(MessageListener):

    def __init__(self, learning_rate, model, random_seed=42):
        _LOGGER.info("Creating ParameterServer with LR: {}".format(learning_rate))
        self.learning_rate = learning_rate
        self.model = model
        self.parameter_shard = torch.rand(ravel_model_params(model).numel())
        self.log_dataframe = []
        #init superclass
        super().__init__(model)

    def receive(self, sender, message_code, parameter):
        _LOGGER.info("Processing message: {} from sender {}".format(message_code.name, sender))

        if message_code == MessageCode.ParameterUpdate:
            #be sure to clone here
            self.parameter_shard = parameter.clone()

        elif message_code == MessageCode.ParameterRequest:
            send_message(MessageCode.ParameterUpdate, self.parameter_shard, dst=sender)    

        elif message_code == MessageCode.GradientUpdate:
            self.parameter_shard -= self.learning_rate * parameter
            unravel_model_params(self.model, self.parameter_shard)

        elif message_code == MessageCode.EvaluateParams:
            evaluate(self.log_dataframe)
    

def init_server():
    model = Net()
    server = ParameterServer(learning_rate=DEFAULT_LEARNING_RATE, model=model)
    server.run()

if __name__ == "__main__":
     init_processes(0, 3, init_server)
