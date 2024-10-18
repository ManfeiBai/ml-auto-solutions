# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities to construct configs for vLLM benchmark DAG."""


import datetime
import json
import os
from typing import Dict
from xlml.apis import gcp_config, metric_config, task, test_config
from dags import test_owner
from dags.multipod.configs import common
from dags.vm_resource import MachineVersion, ImageFamily, ImageProject, GpuVersion, TpuVersion, Project, RuntimeVersion, Zone


PROJECT_NAME = Project.CLOUD_ML_AUTO_SOLUTIONS.value
RUNTIME_IMAGE = RuntimeVersion.TPU_UBUNTU2204_BASE.value
GCS_SUBFOLDER_PREFIX = test_owner.Team.SOLUTIONS_TEAM.value
HF_TOKEN = os.getenv("HF_TOKEN", None)


def get_vllm_gpu_setup_cmds():
  setup_cmds = (
      "pip install --upgrade pip",
      "sudo apt-get -y update",
      "sudo apt install python3",
      "sudo apt-get install python-is-python3",
      "pip install google-auth",
      "pip install vllm",
      "export PATH=$PATH:/home/cloud-ml-auto-solutions/.local/bin",
      "ls $(which vllm)",
      # Download dataset
      "wget --no-verbose https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/ShareGPT_V3_unfiltered_cleaned_split.json",
      # Download benchmark
      "rm -rf ai-on-gke && git clone https://github.com/GoogleCloudPlatform/ai-on-gke",
  )
  return setup_cmds


def get_vllm_tpu_setup_cmds():
  setup_cmds = (
      # Update environment and installs basic deps
      "pip install --upgrade pip",
      "sudo apt-get -y update",
      "sudo apt install -y libopenblas-base libopenblas-dev",
      "sudo apt-get -y install python3.10-venv",
      "sudo apt-get -y install jq",
      "python -m venv .env",
      "source .env/bin/activate",
      # Install vllm at head
      "rm -rf vllm && git clone https://github.com/vllm-project/vllm",
      "cd vllm",
      # From https://docs.vllm.ai/en/latest/getting_started/tpu-installation.html
      "pip uninstall torch torch-xla -y",
      'export DATE="20240828"',
      'export TORCH_VERSION="2.5.0"',
      "pip install https://storage.googleapis.com/pytorch-xla-releases/wheels/tpuvm/torch-${TORCH_VERSION}.dev${DATE}-cp310-cp310-linux_x86_64.whl",
      "pip install https://storage.googleapis.com/pytorch-xla-releases/wheels/tpuvm/torch_xla-${TORCH_VERSION}.dev${DATE}-cp310-cp310-linux_x86_64.whl",
      # Install JAX and Pallas.
      "pip install torch_xla[tpu] -f https://storage.googleapis.com/libtpu-releases/index.html",
      "pip install torch_xla[pallas] -f https://storage.googleapis.com/jax-releases/jax_nightly_releases.html -f https://storage.googleapis.com/jax-releases/jaxlib_nightly_releases.html",
      # Install other build dependencies.
      "pip install setuptools-scm>=8",
      "pip install -r requirements-tpu.txt",
      # Build vLLM
      'VLLM_TARGET_DEVICE="tpu" python setup.py develop',
      # Download dataset
      "cd .. && wget --no-verbose https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/ShareGPT_V3_unfiltered_cleaned_split.json",
      # Download benchmark
      "rm -rf ai-on-gke && git clone https://github.com/GoogleCloudPlatform/ai-on-gke",
  )

  return setup_cmds


def get_vllm_benchmark_cmds(
    model_id: str, num_chips: int, test_run_id: str, model_configs: Dict = {}
):
  base_model_id = model_id.split("/")[-1]
  request_rates = model_configs["request_rates"].split(",")
  instance_type = model_configs["instance_type"]
  num_prompts = 1000

  run_cmds = [
      "export PATH=$PATH:/home/cloud-ml-auto-solutions/vllm:/home/cloud-ml-auto-solutions/.local/bin",
      # HF_TOKEN is set in Composer environment variables
      f"export HF_TOKEN={HF_TOKEN}",
      # Start virtual environment
      '[[ -f ".env/bin/activate" ]] && source .env/bin/activate',
      # Start vllm in the background
      f"vllm serve {model_id} --swap-space 16  --disable-log-requests --tensor_parallel_size={num_chips} --max-model-len=2048 --num-scheduler-steps=4 &",
      # Wait for server to come up
      "sleep 600",
  ]

  # Group metrics together using test_run_id.
  metadata = {
      "test_run_id": test_run_id,
      "instance_type": instance_type,
      "num_accelerators": num_chips,
  }
  for request_rate in request_rates:
    benchmark_cmd_fmt = "python ai-on-gke/benchmarks/benchmark/tools/profile-generator/container/benchmark_serving.py --host localhost --port 8000 --num-prompts {num_prompts} --max-input-length 1024 --max-output-length 1024 --dataset ShareGPT_V3_unfiltered_cleaned_split.json --save-json-results --model '{model_id}' --tokenizer '{model_id}' --request-rate {request_rate} --additional-metadata-metrics-to-save '{additional_metadata}'"

    benchmark_cmds = [
        # Run benchmark
        benchmark_cmd_fmt.format(
            num_prompts=num_prompts,
            model_id=model_id,
            request_rate=request_rate,
            additional_metadata=json.dumps(metadata),
        ),
        # Process result json files
        f'export OUTPUT_FORMAT="*vllm*{base_model_id}*"',
        "export BENCHMARK_OUTPUT=$(find . -name $OUTPUT_FORMAT -type f -printf \"%T@ %Tc %p\n\" | sort -n | head -1 | awk 'NF>1{print $NF}')",
        # Log output file contest
        "cat ${BENCHMARK_OUTPUT}",
        # Append output file contents to final metrics report
        "cat ${BENCHMARK_OUTPUT} >> metric_report.jsonl",
        "echo '' >> metric_report.jsonl",
        "rm ${BENCHMARK_OUTPUT}",
    ]
    run_cmds.extend(benchmark_cmds)

  run_cmds.extend([
      # Kill background process
      "pkill -P $$",
      # Copy metrics as the last step
      f"gsutil cp metric_report.jsonl {metric_config.SshEnvVars.GCS_OUTPUT.value}",
  ])

  return tuple(run_cmds)


def get_gpu_vllm_gce_config(
    machine_version: MachineVersion,
    image_project: ImageProject,
    image_family: ImageFamily,
    gpu_version: GpuVersion,
    count: int,
    backend: str,
    gpu_zone: Zone,
    time_out_in_min: int,
    test_name: str,
    test_run_id: str,
    project: Project,
    network: str,
    subnetwork: str,
    model_configs: Dict = {},
):
  job_gcp_config = gcp_config.GCPConfig(
      project_name=project.value,
      zone=gpu_zone.value,
      dataset_name=metric_config.DatasetOption.BENCHMARK_DATASET,
  )

  set_up_cmds = get_vllm_gpu_setup_cmds()
  model_configs["instance_type"] = machine_version.value

  run_model_cmds = get_vllm_benchmark_cmds(
      model_id=model_configs["model_id"],
      num_chips=count,
      test_run_id=test_run_id,
      model_configs=model_configs,
  )

  job_test_config = test_config.GpuVmTest(
      test_config.Gpu(
          machine_type=machine_version.value,
          image_family=image_family.value,
          count=count,
          accelerator_type=gpu_version.value,
          runtime_version=RUNTIME_IMAGE,
          network=network,
          subnetwork=subnetwork,
      ),
      test_name=test_name,
      set_up_cmds=set_up_cmds,
      run_model_cmds=run_model_cmds,
      timeout=datetime.timedelta(minutes=time_out_in_min),
      task_owner=test_owner.RICHARD_L,
      gcs_subfolder=f"{GCS_SUBFOLDER_PREFIX}/vllm_benchmark",
  )

  job_gcp_config = gcp_config.GCPConfig(
      project_name=project.value,
      zone=gpu_zone.value,
      dataset_name=metric_config.DatasetOption.BENCHMARK_DATASET,
  )

  job_metric_config = metric_config.MetricConfig(
      json_lines=metric_config.JSONLinesConfig("metric_report.jsonl"),
      use_runtime_generated_gcs_folder=True,
  )

  return task.GpuCreateResourceTask(
      image_project.value,
      image_family.value,
      task_test_config=job_test_config,
      task_gcp_config=job_gcp_config,
      task_metric_config=job_metric_config,
      install_nvidia_drivers=True,
  )


def get_tpu_vllm_gce_config(
    tpu_version: TpuVersion,
    tpu_cores: int,
    tpu_zone: Zone,
    backend: str,
    time_out_in_min: int,
    test_name: str,
    test_run_id: str,
    project: Project,
    runtime_version: str = RUNTIME_IMAGE,
    network: str = "default",
    subnetwork: str = "default",
    is_tpu_reserved: bool = True,
    num_slices: int = 1,
    model_configs: Dict = {},
):
  job_gcp_config = gcp_config.GCPConfig(
      project_name=project.value,
      zone=tpu_zone.value,
      dataset_name=metric_config.DatasetOption.BENCHMARK_DATASET,
  )

  set_up_cmds = get_vllm_tpu_setup_cmds()
  model_configs["instance_type"] = tpu_version.value

  run_model_cmds = get_vllm_benchmark_cmds(
      model_id=model_configs["model_id"],
      num_chips=tpu_cores,
      test_run_id=test_run_id,
      model_configs=model_configs,
  )

  job_test_config = test_config.TpuVmTest(
      test_config.Tpu(
          version=tpu_version,
          cores=tpu_cores,
          runtime_version=runtime_version,
          reserved=is_tpu_reserved,
          network=network,
          subnetwork=subnetwork,
      ),
      test_name=test_name,
      set_up_cmds=set_up_cmds,
      run_model_cmds=run_model_cmds,
      timeout=datetime.timedelta(minutes=time_out_in_min),
      task_owner=test_owner.RICHARD_L,
      num_slices=num_slices,
      gcs_subfolder=f"{GCS_SUBFOLDER_PREFIX}/vllm_benchmark",
  )

  job_metric_config = metric_config.MetricConfig(
      json_lines=metric_config.JSONLinesConfig("metric_report.jsonl"),
      use_runtime_generated_gcs_folder=True,
  )

  return task.run_queued_resource_test(
      task_test_config=job_test_config,
      task_gcp_config=job_gcp_config,
      task_metric_config=job_metric_config,
  )