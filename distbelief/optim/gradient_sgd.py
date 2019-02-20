import sys

import logging
import os
import threading
import torch
import torch.distributed as dist
from datetime import datetime
from queue import Queue
from torch.optim.optimizer import Optimizer, required

from distbelief.utils.messaging import send_message, GSMessageCode, GradientMessageListener
from distbelief.utils.serialization import ravel_model_params, update_model_params, unravel_model_params, \
    ravel_sparse_gradient, unravel_sparse_gradient, worker_gradient_executor

WORKPATH = os.path.abspath(os.path.dirname(os.path.dirname('main.py')))
sys.path.append(WORKPATH)

_LOGGER = logging.getLogger(__name__)
lock = threading.Lock()


class GradientListener(GradientMessageListener):
    """DownpourListener"""

    def __init__(self, model, queue):
        super(GradientListener, self).__init__(model)
        self.lr = 0.05
        self.queue = queue
        self.version = 0
        self.filter_gradient = ravel_model_params(model)

    def receive(self, sender, message_code, gradient_version, parameter, ):
        """receive parameter updates from the server and reflect them into the client's model."""
        _LOGGER.info("Processing message: {}, version: {}, lr: {}".format(message_code.name, gradient_version, self.lr))
        # print("Processing message: {}, version: {}, lr: {}".format(message_code.name, gradient_version, self.lr))
        if message_code == GSMessageCode.GradientUpdate:
            update_model_params(self.model, parameter, -1)
            self.version = gradient_version
            self.queue.put(gradient_version)
        elif message_code == GSMessageCode.SparseGradientUpdate:
            parameter = unravel_sparse_gradient(parameter)
            update_model_params(self.model, parameter, -1)
            self.version = gradient_version
            self.queue.put(gradient_version)
        elif message_code == GSMessageCode.ModelRequest:
            model = ravel_model_params(self.model, grads=False)
            print(model)
            send_message(GSMessageCode.ModelUpdate, model, dst=0, gradient_version=0)
            print('send model to server')
        elif message_code == GSMessageCode.ModelUpdate:
            print('version:', gradient_version, ' ', datetime.now(), ' synced model :', parameter)
            unravel_model_params(self.model, parameter.clone())
            self.version = gradient_version
            print('sync model!')
            self.flag = True
            # TODO change back
            if self.version > 0:
                self.queue.put(gradient_version)
            # lock.release()


class GradientSGD(Optimizer):
    """GradientSGD"""

    def __init__(self, params, lr=required, n_push=0, n_pull=0, model=required):
        """
        :param params:
        :param lr:
        :param n_push:
        :param n_pull:
        :param model:
        """
        if lr is not required and lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))
        print('I am node rank:%d' % dist.get_rank())
        defaults = dict(lr=lr, )
        self.accumulated_gradients = torch.zeros(ravel_model_params(model, cuda=True).size())
        self.model = model
        # this sets the initial model parameters
        # send_message(MessageCode.ParameterUpdate, ravel_model_params(self.model))
        self.idx = 0
        self.version = 0
        self.queue = Queue(maxsize=1)
        self.listener = GradientListener(self.model, self.queue)
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

        if not self.listener.flag:
            return loss
        # increase version No.
        # self.version += 1
        # if dist.get_rank() == 1:
        #     time.sleep(0.01)

        # get the lr
        lr = self.param_groups[0]['lr']
        self.listener.lr = lr

        # keep track of accumulated gradients so that we can send 
        # gradients = ravel_model_params(self.model, grads=True)
        # mp_gradient_filter(self.model)
        # raveled_gradients = ravel_model_params(self.model, grads=True, cuda=True).mul_(lr)
        raveled_gradients = worker_gradient_executor(self.model, rate=0.01, lr=lr)
        # send_message(GSMessageCode.GradientUpdate, raveled_gradients, dst=0,
        #              gradient_version=self.listener.version + 1)
        sparse_gradient = ravel_sparse_gradient(raveled_gradients)
        send_message(GSMessageCode.SparseGradientUpdate, sparse_gradient, dst=0,
                     gradient_version=self.listener.version + 1)

        # reset gradient version
        self.version = self.queue.get()
        # print(self.version)
        self.idx += 1
        return loss
