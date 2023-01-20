# ⚠️ Work In Progress

### This pipeline currently contains bugs that will give inaccurate results.

### It is only suitable for testing and development until those bugs are fixed.

# marple-pgt
Pipeline for analysis of Puccinia graminis f. sp. tritici genomic reads

## Install

Before installing, ensure the licences of miniconda and software dependencies in `env.yml` are compatible with your usage.

The installer:

- Installs conda (miniconda)
- Installs dependencies from `eny.yml` into the environment `marple-pgt`
<!-- - Runs tests with `test/run_tests.sh` -->

```bash
./install/install.sh
```

For WSL, you should keep the `marple-pgt` directory in the Linux file sytem (e.g. `~/marple-pgt`), and not the windows file system (e.g. ~~`/mnt/c/Users/me/Documents/marple-pgt`~~) as you may get an error:

`OSError: [Errno 40] Too many levels of symbolic links`

## Tutorial

If you're starting from multiple FASTQ files for each sample from basecalling then use `transfer_reads.sh` to combine them into a single fastq file:
```bash
src/transfer_reads.sh example barcode01=cz2 barcode02=uk20
```

Let's go through the pipeline using a fictional example.

In this example, 2 samples have already been sequenced (using nanpore sequencing) and basecalled using Guppy.

Our starting point is a directory for each sample containing the fastq files concatenated together after being created by Guppy.

|reads directory        |
|-----------------------|
|example/cz2/cz2.fastq  |
|example/uk20/uk20.fastq|

1. Reset the example and navigate to the example directory

    ```bash
    ./reset_example.sh
    cd example
    ```

2. Align the reads for each sample to the reference genes, extract the CDS, and create a report

    ```bash
    ../src/reads_to_cds_concat.sh --trim no uk20/uk20.fastq cz2/cz2.fastq 
    ```

    The `--trim no` is used to skip adapter trimming by Porechop to save time for the example.

3. Inspect the report by opening `example/multiqc_report.html` in your browser.

4. Use the extracted and concatenated CDS of the new samples to create a tree. For the example create a **very small tree** (takes ~5 minutes) by running:

    ```bash
    ../src/cds_concat_to_tree_imgs.sh \
        --start ../data/12_samples_276_genes_cds.fa.gz \
        --out_dir tree \
        --name 2_new_samples \
        */*_cds_concat.fasta
    ```

    If you were using real data, you could make the **full tree** by removing the `--start` parameter like so. This can take hours to run.

    ```bash
    ../src/cds_concat_to_tree_imgs.sh \
        --out_dir tree \
        --name 2_new_samples \
        */*_cds_concat.fasta
    ```

5. Inspect the tree by opening `example/tree/2_new_samples_country.pdf` in your browser.

    Notice how country is shown as '?' for the new samples. We'll fix that in the next step.

6. Copy the metadata spreadsheet `data/metadata_92.xlsx` and rename it so that you have `example\metadata_92_2_new_samples.xlsx`.

    Add the new sample metadata to the spreadsheet so that the top 3 rows look like this:

    |tree_name|tree_new_name|country|region|clade|accession|year|...|
    |---------|-------------|-------|------|-----|---------|----|---|
    |cz2      |cz2          |Czechia|Europe|     |         |2013|...|
    |uk20     |uk20         |UK     |Europe|     |         |2022|...|

7. Use this metadata to make a new visualisation of the tree

    ```bash
    ../src/tree_to_imgs.sh \
        --meta metadata_92_2_new_samples.xlsx \
        --out_dir tree_with_new_metadata \
        tree/RAxML_bestTree.2_new_samples.newick
    ```

8. Inspect the new visualisation of the tree by opening `example/tree_with_new_metadata/2_new_samples_country.pdf` in your browser.

    The countries of the 2 new samples are now showing correctly.
