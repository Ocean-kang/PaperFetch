#!/bin/bash
cd /root/code/PaperFetch || exit
source /root/miniconda3/etc/profile.d/conda.sh
conda activate paperfetch
/root/miniconda3/envs/paperfetch/bin/python \
    /root/code/PaperFetch/PaperFrech_daily_keyword.py \
    >> /root/code/PaperFetch/log/run.log 2>&1