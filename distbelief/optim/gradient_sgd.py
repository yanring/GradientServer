import time

import logging
import torch
import torch.distributed as dist
from queue import Queue
from torch.optim.optimizer import Optimizer, required

from distbelief.utils.messaging import send_message, GSMessageCode, GradientMessageListener
from distbelief.utils.serialization import ravel_model_params, update_model_params

_LOGGER = logging.getLogger(__name__)


class WorkerGradientWarehouse(object):
    """
    store gradient for fast-sync mechanism
    """

    def __init__(self):
        self.gradient_storage = {}

    def push(self, gradient, version):
        self.gradient_storage[version] = gradient.clone()

    def remove(self, version):
        self.gradient_storage.pop(version)

    def pop(self, version):
        return self.gradient_storage.pop(version)

    def clean_redundant(self, bound=20):
        try:
            cur_version = max(self.gradient_storage.keys())
        except Exception as e:
            return
        key_list = list(self.gradient_storage.keys())
        for i in key_list:
            if cur_version - bound > i:
                self.remove(i)


class GradientListener(GradientMessageListener):
    """DownpourListener"""

    def __init__(self, model, queue, gradient_warehouse):
        super(GradientListener, self).__init__(model)
        self.lr = 0.05
        self.queue = queue
        self.version = 0
        self.tmp_tensor = None
        self.gradient_warehouse = gradient_warehouse

    def receive(self, sender, message_code, gradient_version, trigger, fast_flag, parameter):
        """receive parameter updates from the server and reflect them into the client's model."""
        _LOGGER.info("Processing message: {}, version: {}, lr: {}".format(message_code.name, gradient_version, self.lr))
        if message_code == GSMessageCode.GradientUpdate:
            update_model_params(self.model, parameter, self.lr)
            if not fast_flag:
                # means this version of gradient should not stored by worker cuz this worker is not a fast-node
                self.gradient_warehouse.remove(self.version + 1)
            # print(len(self.gradient_warehouse.gradient_storage), self.gradient_warehouse.gradient_storage.keys())
            self.version = gradient_version
            if trigger is not 0:
                # received lower nodes' gradient
                update_model_params(self.model, self.gradient_warehouse.pop(trigger), -self.lr)
                print("Sync-fast, Received version %d from other nodes" % trigger)
            self.queue.put(gradient_version)


class GradientSGD(Optimizer):
    """GradientSGD"""

    def __init__(self, params, lr=required, n_push=0, n_pull=0, model=required):
        """__init__

        :param params:
        :param lr:
        :param freq:
        :param model:
        """
        if lr is not required and lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))
        print('I am node rank:%d' % dist.get_rank())
        defaults = dict(lr=lr, )
        self.accumulated_gradients = torch.zeros(ravel_model_params(model).size())
        self.model = model
        # this sets the initial model parameters
        # send_message(MessageCode.ParameterUpdate, ravel_model_params(self.model))
        self.idx = 0
        self.version = 0
        self.queue = Queue(maxsize=1)
        self.gradient_warehouse = WorkerGradientWarehouse()
        self.listener = GradientListener(self.model, self.queue, self.gradient_warehouse)
        self.listener.start()

        super(GradientSGD, self).__init__(params, defaults)

    def step(self, closure=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        # increase version No.
        # self.version += 1
        if dist.get_rank() == 1:
            time.sleep(0.03)

        # get the lr
        lr = self.param_groups[0]['lr']
        self.listener.lr = lr

        # keep track of accumulated gradients so that we can send 
        gradients = ravel_model_params(self.model, grads=True)
        self.gradient_warehouse.push(gradients, self.listener.version + 1)
        send_message(GSMessageCode.GradientUpdate, gradients, dst=0, gradient_version=self.listener.version + 1)

        # reset gradient version
        self.queue.get()

        if self.idx % 100 == 1:
            self.gradient_warehouse.clean_redundant()

        self.idx += 1
        return loss