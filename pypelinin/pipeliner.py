# coding: utf-8

#TODO: in future, pipeliner could be a worker in a broker tagged as pipeliner,
#      but router needs to support broker tags

from time import time
from uuid import uuid4
from . import Client


class Pipeliner(Client):
    #TODO: should send monitoring information?
    #TODO: should receive and handle a 'job error' from router when some job
    #      could not be processed (timeout, worker not found etc.)

    def __init__(self, api, broadcast, logger=None, poll_time=50):
        super(Pipeliner, self).__init__()
        self._api_address = api
        self._broadcast_address = broadcast
        self.logger = logger
        self.poll_time = poll_time
        self._new_pipelines = None
        self._messages = []
        self._pipelines = {}
        self._jobs = {}
        self.logger.info('Pipeliner started')

    def start(self):
        try:
            self.connect(self._api_address, self._broadcast_address)
            self.broadcast_subscribe('new pipeline')
            self.run()
        except KeyboardInterrupt:
            self.logger.info('Got SIGNINT (KeyboardInterrupt), exiting.')
            self.disconnect()

    def _update_broadcast(self):
        if self.broadcast_poll(self.poll_time):
            message = self.broadcast_receive()
            self.logger.info('Received from broadcast: {}'.format(message))
            if message.startswith('new pipeline'):
                if self._new_pipelines is None:
                    self._new_pipelines = 1
                else:
                    self._new_pipelines += 1
            else:
                self._messages.append(message)

    def router_has_new_pipeline(self):
        self._update_broadcast()
        return self._new_pipelines > 0

    def ask_for_a_pipeline(self):
        self.send_api_request({'command': 'get pipeline'})
        message = self.get_api_reply()
        #TODO: if router stops and doesn't answer, pipeliner will stop here
        if 'workers' in message and 'data' in message:
            if message['data'] is not None:
                self.logger.info('Got this pipeline: {}'.format(message))
                if self._new_pipelines is None:
                    self._new_pipelines = 0
                else:
                    self._new_pipelines -= 1
                return message
            else:
                self._new_pipelines = 0
        elif 'pipeline' in message and message['pipeline'] is None:
            self.logger.info('Bad bad router, no pipeline for me.')
            return None
        else:
            self.logger.info('Ignoring malformed pipeline: {}'.format(message))
            #TODO: send a 'rejecting pipeline' request to router
            return None

    def get_a_pipeline(self):
        pipeline_definition = 42
        while pipeline_definition is not None:
            pipeline_definition = self.ask_for_a_pipeline()
            if pipeline_definition is not None:
                self.start_pipeline(pipeline_definition)

    def _send_job(self, worker):
        job_request = {'command': 'add job', 'worker': worker.name,
                       'data': worker.data}
        self.send_api_request(job_request)
        self.logger.info('Sent job request: {}'.format(job_request))
        message = self.get_api_reply()
        self.logger.info('Received from router API: {}'.format(message))
        self._jobs[message['job id']] = worker
        subscribe_message = 'job finished: {}'.format(message['job id'])
        self.broadcast_subscribe(subscribe_message)
        self.logger.info('Subscribed on router broadcast to: {}'\
                         .format(subscribe_message))

    def start_pipeline(self, pipeline_definition):
        pipeline = Worker.from_dict(pipeline_definition['workers'])
        pipeline.pipeline_id = pipeline_definition['pipeline id']
        pipeline.data = pipeline_definition['data']
        pipeline.pipeline_started_at = time()
        self._pipelines[pipeline.pipeline_id] = [pipeline]
        self._send_job(pipeline)

    def verify_jobs(self):
        self._update_broadcast()
        new_messages = []
        for message in self._messages:
            if message.startswith('job finished: '):
                job_id = message.split(': ')[1].split(' ')[0]
                if job_id in self._jobs:
                    self.logger.info('Processing finished job id {}.'.format(job_id))
                    worker = self._jobs[job_id]
                    self._pipelines[worker.pipeline_id].remove(worker)
                    next_workers = worker.after
                    for next_worker in next_workers:
                        self.logger.info('   worker after: {}'.format(next_worker.name))
                        next_worker.data = worker.data
                        next_worker.pipeline_id = worker.pipeline_id
                        next_worker.pipeline_started_at = worker.pipeline_started_at
                        self._pipelines[worker.pipeline_id].append(next_worker)
                        self._send_job(next_worker)
                    del self._jobs[job_id]
                    if not self._pipelines[worker.pipeline_id]:
                        total_time = time() - worker.pipeline_started_at
                        self.logger.info('Finished pipeline {}'\
                                         .format(worker.pipeline_id))
                        self.send_api_request({'command': 'pipeline finished',
                                'pipeline id': worker.pipeline_id,
                                'duration': total_time})
                        self.get_api_reply()
                        #TODO: check reply
                        del self._pipelines[worker.pipeline_id]
                        self.get_a_pipeline()
                self.broadcast_unsubscribe(message)
        self._messages = []

    def run(self):
        self.logger.info('Entering main loop')
        self.get_a_pipeline()
        while True:
            if self.router_has_new_pipeline():
                self.get_a_pipeline()
            self.verify_jobs()
