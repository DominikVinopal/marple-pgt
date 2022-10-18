#!/bin/bash

[ -z $SLURM_JOBID ] && is_on_slurm=false || is_on_slurm=true

$is_on_slurm || script=$0
$is_on_slurm && script=$(scontrol show job $SLURM_JOBID | awk -F= '/Command=/{print $2}' | cut -d" " -f1)
src_dir=$(cd $(dirname "$script"); pwd)

# Bash strict mode
set -euo pipefail

marple_pst_dir=$( dirname "$src_dir" )
source "$marple_pst_dir"/marple_pgt_miniconda/bin/activate marple-pgt
python3 "$src_dir"/cds_concat_to_tree_imgs.py $@
