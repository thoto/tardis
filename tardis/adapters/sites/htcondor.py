from ...configuration.configuration import Configuration
from ...exceptions.executorexceptions import CommandExecutionFailure
from ...exceptions.tardisexceptions import TardisError
from ...exceptions.tardisexceptions import TardisResourceStatusUpdateFailed
from ...interfaces.siteadapter import SiteAdapter
from ...interfaces.siteadapter import ResourceStatus
from ...utilities.asynccachemap import AsyncCacheMap
from ...utilities.attributedict import AttributeDict
from ...utilities.staticmapping import StaticMapping
from ...utilities.executors.shellexecutor import ShellExecutor

from contextlib import contextmanager
from datetime import datetime
from functools import partial
from io import StringIO

import csv
import logging
import re


async def htcondor_queue_updater(executor):
    attributes = dict(Owner="Owner", JobStatus="JobStatus", ClusterId="ClusterId", ProcId="ProcId")
    attributes_string = " ".join(attributes.values())
    queue_command = f"condor_q -af:t {attributes_string}"

    htcondor_queue = {}
    try:
        condor_queue = await executor.run_command(queue_command)
    except CommandExecutionFailure as cf:
        logging.error(f"htcondor_queue_update failed: {cf}")
        raise
    else:
        with StringIO(condor_queue.stdout) as csv_input:
            cvs_reader = csv.DictReader(csv_input, fieldnames=tuple(attributes.keys()), delimiter='\t')
            for row in cvs_reader:
                htcondor_queue[int(row['ClusterId'])] = row
        return htcondor_queue


class HTCondorSiteAdapter(SiteAdapter):
    def __init__(self, machine_type, site_name):
        self.configuration = getattr(Configuration(), site_name)
        self._machine_type = machine_type
        self._site_name = site_name
        self._executor = getattr(self.configuration, 'executor', ShellExecutor())

        key_translator = StaticMapping(resource_id='ClusterId', resource_status='JobStatus',
                                       created='created', updated='updated')

        # HTCondor uses digits to indicate job states and digit as variable names are not allowed in Python, therefore
        # the trick using an expanded dictionary is necessary. Somehow ugly.
        translator_functions = StaticMapping(ClusterId=lambda x: int(x),
                                             JobStatus=lambda x,
                                             translator=StaticMapping(**{'0': ResourceStatus.Error,
                                                                         '1': ResourceStatus.Booting,
                                                                         '2': ResourceStatus.Running,
                                                                         '3': ResourceStatus.Stopped,
                                                                         '4': ResourceStatus.Deleted,
                                                                         '5': ResourceStatus.Error,
                                                                         '6': ResourceStatus.Error}):
                                             translator[x])

        self.handle_response = partial(self.handle_response, key_translator=key_translator,
                                       translator_functions=translator_functions)

        self._htcondor_queue = AsyncCacheMap(update_coroutine=partial(htcondor_queue_updater, self._executor),
                                             max_age=self.configuration.max_age * 60)

    async def deploy_resource(self, resource_attributes):
        submit_command = f"condor_submit {self.configuration.jdl}"
        response = await self._executor.run_command(submit_command)
        pattern = re.compile(r"^.*?(?P<Jobs>\d+).*?(?P<ClusterId>\d+).$", flags=re.MULTILINE)
        response = AttributeDict(pattern.search(response.stdout).groupdict())
        now = datetime.now()
        response.update(AttributeDict(created=now, updated=now))
        return self.handle_response(response)

    @property
    def machine_meta_data(self):
        return self.configuration.MachineMetaData[self._machine_type]

    @property
    def machine_type(self):
        return self._machine_type

    @property
    def site_name(self):
        return self._site_name

    async def resource_status(self, resource_attributes):
        await self._htcondor_queue.update_status()
        try:
            resource_status = self._htcondor_queue[resource_attributes.resource_id]
        except KeyError:
            # In case the created timestamp is after last update timestamp of the asynccachemap,
            # no decision about the current state can be given, since map is updated asynchronously.
            if (self._htcondor_queue.last_update - resource_attributes.created).total_seconds() < 0:
                raise TardisResourceStatusUpdateFailed
            else:
                return AttributeDict(resource_status=ResourceStatus.Deleted)
        else:
            return self.handle_response(resource_status)

    async def stop_resource(self, resource_attributes):
        """"Stopping machines is not supported in HTCondor, therefore terminate is called!"""
        return await self.terminate_resource(resource_attributes)

    async def terminate_resource(self, resource_attributes):
        terminate_command = f"condor_rm {resource_attributes.resource_id}"
        response = self._executor.run_command(terminate_command)
        return response

    @contextmanager
    def handle_exceptions(self):
        try:
            yield
        except TardisResourceStatusUpdateFailed:
            raise
        except Exception as ex:
            raise TardisError from ex