#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""A set of utilities to write pipelines for performance tests.

xThis module offers a way to create pipelines using synthetic sources and steps.
Exact shape of the pipeline and the behaviour of sources and steps can be
controlled through arguments. Please see function 'parse_args()' for more
details about the arguments.

Shape of the pipeline is primariy controlled through two arguments. Argument
'steps' can be used to define a list of steps as a JSON string. Argument
'barrier' describes how these steps are separated from each other. Argument
'barrier' can be use to build a pipeline as a a series of steps or a tree of
steps with a fanin or a fanout of size 2.

Other arguments describe what gets generated by synthetic sources that produce
data for the pipeline.
"""

from __future__ import absolute_import
from __future__ import division

import argparse
import json
import logging
import math
import time

import apache_beam as beam
from apache_beam.io import WriteToText
from apache_beam.io import iobase
from apache_beam.io import range_trackers
from apache_beam.io import restriction_trackers
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.transforms.core import RestrictionProvider

try:
  import numpy as np
except ImportError:
  np = None


def parse_byte_size(s):
  suffixes = 'BKMGTP'
  if s[-1] in suffixes:
    return int(float(s[:-1]) * 1024**suffixes.index(s[-1]))

  return int(s)


def div_round_up(a, b):
  """Return ceil(a/b)."""
  return int(math.ceil(float(a) / b))


def rotate_key(element):
  """Returns a new key-value pair of the same size but with a different key."""
  (key, value) = element
  return key[-1:] + key[:-1], value


class SyntheticStep(beam.DoFn):
  """A DoFn of which behavior can be controlled through prespecified parameters.
  """

  def __init__(self, per_element_delay_sec=0, per_bundle_delay_sec=0,
               output_records_per_input_record=1, output_filter_ratio=0):
    if per_element_delay_sec and per_element_delay_sec < 1e-3:
      raise ValueError('Per element sleep time must be at least 1e-3. '
                       'Received: %r', per_element_delay_sec)
    self._per_element_delay_sec = per_element_delay_sec
    self._per_bundle_delay_sec = per_bundle_delay_sec
    self._output_records_per_input_record = output_records_per_input_record
    self._output_filter_ratio = output_filter_ratio

  def start_bundle(self):
    self._start_time = time.time()

  def finish_bundle(self):
    # The target is for the enclosing stage to take as close to as possible
    # the given number of seconds, so we only sleep enough to make up for
    # overheads not incurred elsewhere.
    to_sleep = self._per_bundle_delay_sec - (time.time() - self._start_time)

    # Ignoring sub-millisecond sleep times.
    if to_sleep >= 1e-3:
      time.sleep(to_sleep)

  def process(self, element):
    if self._per_element_delay_sec >= 1e-3:
      time.sleep(self._per_element_delay_sec)
    filter_element = False
    if self._output_filter_ratio > 0:
      if np.random.random() < self._output_filter_ratio:
        filter_element = True

    if not filter_element:
      for _ in range(self._output_records_per_input_record):
        yield element


class SyntheticSource(iobase.BoundedSource):
  """A custom source of a specified size.
  """

  def __init__(self, input_spec):
    """Initiates a synthetic source.

    Args:
      input_spec: Input specification of the source. See corresponding option in
                  function 'parse_args()' below for more details.
    Raises:
      ValueError: if input parameters are invalid.
    """

    def maybe_parse_byte_size(s):
      return parse_byte_size(s) if isinstance(s, str) else int(s)

    self._num_records = input_spec['numRecords']
    self._key_size = maybe_parse_byte_size(input_spec.get('keySizeBytes', 1))
    self._value_size = maybe_parse_byte_size(
        input_spec.get('valueSizeBytes', 1))
    self._total_size = self.element_size * self._num_records
    self._initial_splitting = (
        input_spec['bundleSizeDistribution']['type']
        if 'bundleSizeDistribution' in input_spec else 'const')
    if self._initial_splitting != 'const' and self._initial_splitting != 'zipf':
      raise ValueError(
          'Only const and zipf distributions are supported for determining '
          'sizes of bundles produced by initial splitting. Received: %s',
          self._initial_splitting)
    self._initial_splitting_num_bundles = (
        input_spec['forceNumInitialBundles']
        if 'forceNumInitialBundles' in input_spec else 0)
    if self._initial_splitting == 'zipf':
      self._initial_splitting_distribution_parameter = (
          input_spec['bundleSizeDistribution']['param'])
      if self._initial_splitting_distribution_parameter < 1:
        raise ValueError(
            'Parameter for a Zipf distribution must be larger than 1. '
            'Received %r.', self._initial_splitting_distribution_parameter)
    else:
      self._initial_splitting_distribution_parameter = 0
    self._dynamic_splitting = (
        'none' if (
            'splitPointFrequencyRecords' in input_spec
            and input_spec['splitPointFrequencyRecords'] == 0)
        else 'perfect')
    if 'delayDistribution' in input_spec:
      if input_spec['delayDistribution']['type'] != 'const':
        raise ValueError('SyntheticSource currently only supports delay '
                         'distributions of type \'const\'. Received %s.',
                         input_spec['delayDistribution']['type'])
      self._sleep_per_input_record_sec = (
          float(input_spec['delayDistribution']['const']) / 1000)
      if (self._sleep_per_input_record_sec and
          self._sleep_per_input_record_sec < 1e-3):
        raise ValueError('Sleep time per input record must be at least 1e-3.'
                         ' Received: %r', self._sleep_per_input_record_sec)
    else:
      self._sleep_per_input_record_sec = 0

  @property
  def element_size(self):
    return self._key_size + self._value_size

  def estimate_size(self):
    return self._total_size

  def split(self, desired_bundle_size, start_position=0, stop_position=None):
    # Performs initial splitting of SyntheticSource.
    #
    # Exact sizes and distribution of initial splits generated here depends on
    # the input specification of the SyntheticSource.

    if stop_position is None:
      stop_position = self._num_records
    if self._initial_splitting == 'zipf':
      desired_num_bundles = self._initial_splitting_num_bundles or math.ceil(
          float(self.estimate_size()) / desired_bundle_size)
      samples = np.random.zipf(self._initial_splitting_distribution_parameter,
                               desired_num_bundles)
      total = sum(samples)
      relative_bundle_sizes = [(float(sample) / total) for sample in samples]
      bundle_ranges = []
      start = start_position
      index = 0
      while start < stop_position:
        if index == desired_num_bundles - 1:
          bundle_ranges.append((start, stop_position))
          break
        stop = start + int(self._num_records * relative_bundle_sizes[index])
        bundle_ranges.append((start, stop))
        start = stop
        index += 1
    else:
      if self._initial_splitting_num_bundles:
        bundle_size_in_elements = max(1, int(
            self._num_records /
            self._initial_splitting_num_bundles))
      else:
        bundle_size_in_elements = (max(
            div_round_up(desired_bundle_size, self.element_size),
            int(math.floor(math.sqrt(self._num_records)))))
      bundle_ranges = []
      for start in range(start_position, stop_position,
                         bundle_size_in_elements):
        stop = min(start + bundle_size_in_elements, stop_position)
        bundle_ranges.append((start, stop))

    for start, stop in bundle_ranges:
      yield iobase.SourceBundle(stop - start, self, start, stop)

  def get_range_tracker(self, start_position, stop_position):
    if start_position is None:
      start_position = 0
    if stop_position is None:
      stop_position = self._num_records
    tracker = range_trackers.OffsetRangeTracker(start_position, stop_position)
    if self._dynamic_splitting == 'none':
      tracker = range_trackers.UnsplittableRangeTracker(tracker)
    return tracker

  def read(self, range_tracker):
    index = range_tracker.start_position()
    while range_tracker.try_claim(index):
      r = np.random.RandomState(index)

      time.sleep(self._sleep_per_input_record_sec)
      yield r.bytes(self._key_size), r.bytes(self._value_size)
      index += 1

  def default_output_coder(self):
    return beam.coders.TupleCoder(
        [beam.coders.BytesCoder(), beam.coders.BytesCoder()])


class SyntheticSDFSourceRestrictionProvider(RestrictionProvider):
  """A `RestrictionProvider` for SyntheticSDFAsSource.

  In initial_restriction(element) and split(element), element means source
  description.
  A typical element is like:

    {
      'key_size': 1,
      'value_size': 1,
      'initial_splitting_num_bundles': 2,
      'initial_splitting_desired_bundle_size': 2,
      'sleep_per_input_record_sec': 0,
      'initial_splitting' : 'const'

    }

  """

  def initial_restriction(self, element):
    return (0, element['num_records'])

  def create_tracker(self, restriction):
    return restriction_trackers.OffsetRestrictionTracker(
        restriction[0], restriction[1])

  def split(self, element, restriction):
    bundle_ranges = []
    start_position, stop_position = restriction
    element_size = element['key_size'] + element['value_size']
    estimate_size = element_size * element['num_records']
    if element['initial_splitting'] == 'zipf':
      desired_num_bundles = (
          element['initial_splitting_num_bundles'] or
          div_round_up(estimate_size,
                       element['initial_splitting_desired_bundle_size']))
      samples = np.random.zipf(
          element['initial_splitting_distribution_parameter'],
          desired_num_bundles)
      total = sum(samples)
      relative_bundle_sizes = [(float(sample) / total) for sample in samples]
      start = start_position
      index = 0
      while start < stop_position:
        if index == desired_num_bundles - 1:
          bundle_ranges.append((start, stop_position))
          break
        stop = start + int(
            element['num_records'] * relative_bundle_sizes[index])
        bundle_ranges.append((start, stop))
        start = stop
        index += 1
    else:
      if element['initial_splitting_num_bundles']:
        bundle_size_in_elements = max(1, int(
            element['num_records'] /
            element['initial_splitting_num_bundles']))
      else:
        bundle_size_in_elements = (max(
            div_round_up(
                element['initial_splitting_desired_bundle_size'], element_size),
            int(math.floor(math.sqrt(element['num_records'])))))
      for start in range(start_position, stop_position,
                         bundle_size_in_elements):
        stop = min(start + bundle_size_in_elements, stop_position)
        bundle_ranges.append((start, stop))
    return bundle_ranges

  def restriction_size(self, element, restriction):
    return ((element['key_size'] + element['value_size'])
            * (restriction[1] - restriction[0]))


class SyntheticSDFAsSource(beam.DoFn):
  """A SDF that generates records like a source.

  This SDF accepts a PCollection of record-based source description.
  A typical description is like:

    {
      'key_size': 1,
      'value_size': 1,
      'initial_splitting_num_bundles': 2,
      'initial_splitting_desired_bundle_size': 2,
      'sleep_per_input_record_sec': 0,
      'initial_splitting' : 'const'

    }

  A simple pipeline taking this SDF as a source is like:
    p
    | beam.Create([description1, description2,...])
    | beam.ParDo(SyntheticSDFAsSource())

  NOTE:
    The SDF.process() will have different param content between defining a DoFn
    and runtime.
    When defining an SDF.process, the restriction_tracker should be a
    `RestrictionProvider`.
    During runtime, the DoFnRunner.process_with_sized_restriction() will feed
    a 'RestrictionTracker' based on a restriction to SDF.process().
  """

  def process(
      self,
      element,
      restriction_tracker=beam.DoFn.RestrictionParam(
          SyntheticSDFSourceRestrictionProvider())):
    for k in range(*restriction_tracker.current_restriction()):
      if not restriction_tracker.try_claim(k):
        return
      r = np.random.RandomState(k)
      time.sleep(element['sleep_per_input_record_sec'])
      yield r.bytes(element['key_size']), r.bytes(element['value_size'])


class ShuffleBarrier(beam.PTransform):

  def expand(self, pc):
    return (pc
            | beam.Map(rotate_key)
            | beam.GroupByKey()
            | 'Ungroup' >> beam.FlatMap(
                lambda elm: [(elm[0], v) for v in elm[1]]))


class SideInputBarrier(beam.PTransform):

  def expand(self, pc):
    return (pc
            | beam.Map(rotate_key)
            | beam.Map(
                lambda elem, ignored: elem,
                beam.pvalue.AsIter(pc | beam.FlatMap(lambda elem: None))))


def merge_using_gbk(name, pc1, pc2):
  """Merges two given PCollections using a CoGroupByKey."""

  pc1_with_key = pc1 | (name + 'AttachKey1') >> beam.Map(lambda x: (x, x))
  pc2_with_key = pc2 | (name + 'AttachKey2') >> beam.Map(lambda x: (x, x))

  grouped = (
      {'pc1': pc1_with_key, 'pc2': pc2_with_key} |
      (name + 'Group') >> beam.CoGroupByKey())
  return (grouped |
          (name + 'DeDup') >> beam.Map(lambda elm: elm[0]))  # Ignoring values


def merge_using_side_input(name, pc1, pc2):
  """Merges two given PCollections using side inputs."""

  def join_fn(val, _):  # Ignoring side input
    return val

  return pc1 | name >> beam.core.Map(join_fn, beam.pvalue.AsIter(pc2))


def expand_using_gbk(name, pc):
  """Expands a given PCollection into two copies using GroupByKey."""

  ret = []
  ret.append((pc | ('%s.a' % name) >> ShuffleBarrier()))
  ret.append((pc | ('%s.b' % name) >> ShuffleBarrier()))
  return ret


def expand_using_second_output(name, pc):
  """Expands a given PCollection into two copies using side outputs."""

  class ExpandFn(beam.DoFn):

    def process(self, element):
      yield beam.pvalue.TaggedOutput('second_out', element)
      yield element

  pc1, pc2 = (pc | name >> beam.ParDo(
      ExpandFn()).with_outputs('second_out', main='main_out'))
  return [pc1, pc2]


def _parse_steps(json_str):
  """Converts the JSON step description into Python objects.

  See property 'steps' for more details about the JSON step description.

  Args:
    json_str: a JSON string that describes the steps.

  Returns:
    Information about steps as a list of dictionaries. Each dictionary may have
    following properties.
    (1) per_element_delay - amount of delay for each element in seconds.
    (2) per_bundle_delay - minimum amount of delay for a given step in seconds.
    (3) output_records_per_input_record - number of output elements generated
        for each input element to a step.
    (4) output_filter_ratio - the probability at which a step may filter out a
        given element by not producing any output for that element.
  """
  all_steps = []
  json_data = json.loads(json_str)
  for val in json_data:
    steps = {}
    steps['per_element_delay'] = (
        (float(val['per_element_delay_msec']) / 1000)
        if 'per_element_delay_msec' in val else 0)
    steps['per_bundle_delay'] = (
        float(val['per_bundle_delay_sec'])
        if 'per_bundle_delay_sec' in val else 0)
    steps['output_records_per_input_record'] = (
        int(val['output_records_per_input_record'])
        if 'output_records_per_input_record' in val else 1)
    steps['output_filter_ratio'] = (
        float(val['output_filter_ratio'])
        if 'output_filter_ratio' in val else 0)
    all_steps.append(steps)

  return all_steps


def parse_args(args):
  """Parses a given set of arguments.

  Args:
    args: set of arguments to be passed.

  Returns:
    a tuple where first item gives the set of arguments defined and parsed
    within this method and second item gives the set of unknown arguments.
  """

  parser = argparse.ArgumentParser()
  parser.add_argument(
      '--steps',
      dest='steps',
      type=_parse_steps,
      help='A JSON string that gives a list where each entry of the list is '
           'configuration information for a step. Configuration for each step '
           'consists of '
           '(1) A float "per_bundle_delay_sec" (in seconds). Defaults to 0.'
           '(2) A float "per_element_delay_msec" (in milli seconds). '
           '    Defaults to 0.'
           '(3) An integer "output_records_per_input_record". Defaults to 1.'
           '(4) A float "output_filter_ratio" in the range [0, 1] . '
           '    Defaults to 0.')

  parser.add_argument(
      '--input',
      dest='input',
      type=json.loads,
      help='A JSON string that describes the properties of the SyntheticSource '
           'used by the pipeline. Configuration is similar to Java '
           'SyntheticBoundedInput.'
           'Currently supports following properties. '
           '(1) An integer "numRecords". '
           '(2) An integer "keySize". '
           '(3) An integer "valueSize". '
           '(4) A tuple "bundleSizeDistribution" with following values. '
           '    A string "type". Allowed values are "const" and "zipf". '
           '    An float "param". Only used if "type"=="zipf". Must be '
           '    larger than 1. '
           '(5) An integer "forceNumInitialBundles". '
           '(6) An integer "splitPointFrequencyRecords". '
           '(7) A tuple "delayDistribution" with following values. '
           '    A string "type". Only allowed value is "const". '
           '    An integer "const". ')

  parser.add_argument('--barrier',
                      dest='barrier',
                      default='shuffle',
                      choices=['shuffle', 'side-input', 'expand-gbk',
                               'expand-second-output', 'merge-gbk',
                               'merge-side-input'],
                      help='Whether to use shuffle as the barrier '
                           '(as opposed to side inputs).')
  parser.add_argument('--output',
                      dest='output',
                      default='',
                      help='Destination to write output.')

  return parser.parse_known_args(args)


def run(argv=None):
  """Runs the workflow."""
  known_args, pipeline_args = parse_args(argv)

  pipeline_options = PipelineOptions(pipeline_args)
  pipeline_options.view_as(SetupOptions).save_main_session = True

  input_info = known_args.input

  with TestPipeline(options=pipeline_options) as p:
    source = SyntheticSource(input_info)

    # pylint: disable=expression-not-assigned
    barrier = known_args.barrier

    pc_list = []
    num_roots = 2 ** (len(known_args.steps) - 1) if (
        barrier == 'merge-gbk' or barrier == 'merge-side-input') else 1
    for read_no in range(num_roots):
      pc_list.append((p | ('Read %d' % read_no) >> beam.io.Read(source)))

    for step_no, steps in enumerate(known_args.steps):
      if step_no != 0:
        new_pc_list = []
        for pc_no, pc in enumerate(pc_list):
          if barrier == 'shuffle':
            new_pc_list.append(
                (pc |
                 ('shuffle %d.%d' % (step_no, pc_no)) >> ShuffleBarrier()))
          elif barrier == 'side-input':
            new_pc_list.append(
                (pc |
                 ('side-input %d.%d' % (step_no, pc_no)) >> SideInputBarrier()))
          elif barrier == 'expand-gbk':
            new_pc_list.extend(
                expand_using_gbk(('expand-gbk %d.%d' % (step_no, pc_no)), pc))
          elif barrier == 'expand-second-output':
            new_pc_list.extend(
                expand_using_second_output(
                    ('expand-second-output %d.%d' % (step_no, pc_no)), pc))
          elif barrier == 'merge-gbk':
            if pc_no % 2 == 0:
              new_pc_list.append(
                  merge_using_gbk(('merge-gbk %d.%d' % (step_no, pc_no)),
                                  pc, pc_list[pc_no + 1]))
            else:
              continue
          elif barrier == 'merge-side-input':
            if pc_no % 2 == 0:
              new_pc_list.append(
                  merge_using_side_input(
                      ('merge-side-input %d.%d' % (step_no, pc_no)),
                      pc, pc_list[pc_no + 1]))
            else:
              continue

        pc_list = new_pc_list

      new_pc_list = []
      for pc_no, pc in enumerate(pc_list):
        new_pc = pc | 'SyntheticStep %d.%d' % (step_no, pc_no) >> beam.ParDo(
            SyntheticStep(
                per_element_delay_sec=steps['per_element_delay'],
                per_bundle_delay_sec=steps['per_bundle_delay'],
                output_records_per_input_record=
                steps['output_records_per_input_record'],
                output_filter_ratio=
                steps['output_filter_ratio']))
        new_pc_list.append(new_pc)
      pc_list = new_pc_list

    if known_args.output:
      # If an output location is provided we format and write output.
      if len(pc_list) == 1:
        (pc_list[0] |
         'FormatOutput' >> beam.Map(lambda elm: (elm[0] + elm[1])) |
         'WriteOutput' >> WriteToText(known_args.output))

  logging.info('Pipeline run completed.')


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  run()
