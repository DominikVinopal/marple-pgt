import tempfile
from collections import defaultdict
from os import makedirs
from os.path import abspath, basename, isfile, join
from re import finditer, sub
from typing import Dict, Iterable, List, Optional, Tuple
import subprocess
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from Bio import Phylo
from Bio.SeqIO import parse
from tempfile import NamedTemporaryFile

from utils import (darken_color, file, get_sample_name_and_extenstion, pushd,
                   run, string_to_color, write_fasta)


# Convert a FASTA with line breaks in sequences to 2 rows per record
def unwrap_fasta(wrapped_fasta: str, unwrapped_fasta: str) -> None:
    with open(unwrapped_fasta, 'wt') as f:
        f.write(''.join(f'>' + r.description + '\n' + str(r.seq) + '\n' for r in parse(wrapped_fasta, 'fasta')))

# Create a FASTA of the CDS region from genes
def extract_cds(genomic_fasta: str, gff: str, out_dir: str) -> str:
    sample_name, sample_ext = get_sample_name_and_extenstion(genomic_fasta, 'fasta')
    cds_fasta = join(out_dir, f'{sample_name}_cds{sample_ext}')
    with NamedTemporaryFile() as wrapped_cds_temp_file:
        run(['gffread', '-x', wrapped_cds_temp_file.name, '-g', genomic_fasta, gff])
        unwrap_fasta(wrapped_cds_temp_file.name, cds_fasta)
    return cds_fasta

# sample.fa  =>  sample_concat.fa
# >foo           >sample   
# ATGATCG        ATGATCGGCTGATC
# >bar      
# GCTGATC       
def fasta_concat(fasta_path: str, out_dir: str) -> str:
    sample_name, sample_ext = get_sample_name_and_extenstion(fasta_path, 'fasta')
    fasta_concat_path = join(out_dir, f'{sample_name}_concat{sample_ext}')
    for suffix_to_drop in ['_cds', '_exons']:
        if sample_name.endswith(suffix_to_drop):
            sample_name = sample_name[:-len(suffix_to_drop)]
    # TODO: figrue out why these CDS are a different length to the existing 
    #       tree input consensus files
    temp_fixes = {
        'rna4054': 'N' * 1655,
        'rna16319': 'N' * 1631,
        'rna4229': 'N' * 1143,
    }
    seq = ''.join(
        temp_fixes.get(r.id, str(r.seq))
        for r in parse(fasta_path, 'fasta')
        # exclude the fungicide genes
        if r.id not in {'mRNA_9243', 'mRNA_44', 'mRNA_1581', 'mRNA_9290'}
    )
    write_fasta({sample_name: seq}, fasta_concat_path)
    return fasta_concat_path

def filter_read_length(
    fastq_in: str,
    fastq_out: str,
    max_read_length: int = 4_000,
) -> None:
    with open(fastq_in) as f_in, open(fastq_out, 'wt') as f_out:
        for i, line in enumerate(f_in):
            if (i % 4) == 0: # sequence identifier line
                if i > 0 and read_length <= max_read_length:
                    f_out.write(record)
                record = ''
            elif (i % 4) == 1: # sequence letters line
                read_length = len(line.strip())
            record += line

# Make a pileup and return the path to it
def reads_to_pileup(
    fastq: str,
    reference: str,
    out_dir: str,
    threads=1,
    trim=True,
    max_read_length: int = 4_000,
) -> str:
    makedirs(out_dir, exist_ok=True)
    sample_name, sample_ext = get_sample_name_and_extenstion(fastq, 'fastq')
    threads = str(threads)
    if trim:
        trimmed = join(out_dir, f'{sample_name}_porechopped{sample_ext}')
        print('trimming', end=' ', flush=True)
        run(['porechop', '--threads', threads, '-i', fastq], trimmed)
    else:
        trimmed = fastq
    filtered = join(out_dir, f'{sample_name}_len_le_{max_read_length}{sample_ext}')
    print('filtering', end=' ', flush=True)
    filter_read_length(trimmed, filtered, max_read_length)
    print('aligning', end=' ', flush=True)
    run(['bwa', 'index', reference])
    aligned_sam = join(out_dir, f'{sample_name}.sam')
    run(['bwa', 'mem', '-t', threads, reference, filtered], aligned_sam)
    aligned_bam = join(out_dir, f'{sample_name}_unsorted.bam')
    run(['samtools', 'view', '-@', threads, '-S', '-b', aligned_sam], aligned_bam)
    sorted_bam = join(out_dir, f'{sample_name}.bam')
    run(['samtools', 'sort', '-@', threads, aligned_bam], sorted_bam)
    run(['samtools', 'faidx', reference])
    pileup = join(out_dir, f'{sample_name}.pileup')
    run(['samtools', 'mpileup', '-f', reference, sorted_bam], pileup)

    # Let the caller know where to find the pileup file
    return pileup

def snp_ratios_and_snp_freq_to_consensus(
    snp_ratios_path: str,
    snp_freq_path: str,
    reference_path: str,
    out_dir: str,
    min_match_depth: int,
):
    sample_name, _ = get_sample_name_and_extenstion(snp_ratios_path, 'snp_ratios')
    
    snp_freq = pd.read_csv(snp_freq_path, sep='\t', header=None)
    snp_freq.columns = [
        'gene', 'pos', 'pos1', 'ref', 'depth', 'allele', 'genotype', 'ratios'
    ]
    # Drop the position where 3+ bases qualified (e.g. T ACT ? 0.2,0.6,0.2)
    snp_freq = snp_freq[snp_freq.genotype != '?']

    snp_ratios = pd.read_csv(snp_ratios_path, sep='\t', header=None)
    snp_ratios.columns = ['gene', 'pos', 'ref', 'depth', 'alts', 'ratios']

    allele_to_code = {
        'AA': 'A',  'CC': 'C',  'GG': 'G',  'TT': 'T',  'UU': 'U',
        'AT': 'W',  'CG': 'S',  'AC': 'M',  'GT': 'K',  'AG': 'R',  'CT': 'Y',
        'TA': 'W',  'GC': 'S',  'CA': 'M',  'TG': 'K',  'GA': 'R',  'TC': 'Y',
    }

    # Start with an empty consensus
    consensus = {str(r.id): ['N'] * len(r.seq) for r in parse(reference_path, 'fasta')}

    # Fill in the consensus using alternate bases where coverage >= min_depth
    for gene, pos, _, allele in snp_freq[['gene', 'pos', 'genotype', 'allele']].values:
        consensus[gene][pos] = allele_to_code.get(allele, consensus[gene][pos])

    # Fill in the consensus using reference matches where coverage >= min_match_depth
    ref_matches = snp_ratios[snp_ratios.alts == snp_ratios.ref]
    ref_matches_enough_depth = ref_matches.query(f'depth >= {min_match_depth}')
    for gene, pos, ref in ref_matches_enough_depth[['gene', 'pos', 'ref']].values:
        # snp_ratios pos comes directly from pileup which is 1-based index
        consensus[gene][pos - 1] = ref

    # Convert the sequence lists into strings
    consensus_fasta = {gene: ''.join(l) for gene, l in consensus.items()}

    # Write out the consensus
    consensus_path = join(out_dir, f'{sample_name}.fasta')
    write_fasta(consensus_fasta, consensus_path)

    # Let the caller know where to find the consensus file
    return consensus_path


# Make a consensus and return the path to it


def pileup_to_consensus(
    pileup_path: str,
    reference_path: str,
    out_dir: str,
    min_snp_depth: int,
    min_match_depth: int,
    hetero_min=.25,
    hetero_max=.75,
) -> str:

    sample_name, _ = get_sample_name_and_extenstion(pileup_path, 'pileup')

    snp_ratios_path = join(out_dir, f'{sample_name}_snp_ratios.tsv')
    pileup_to_snp_ratios(pileup_path, snp_ratios_path)

    snp_freq_path = join(out_dir, f'{sample_name}_snp_freq.tsv')
    snp_ratios_to_snp_freq(
        snp_ratios_path, snp_freq_path, min_snp_depth, hetero_min, hetero_max
    )

    # Let the caller know where to find the consensus file
    return snp_ratios_and_snp_freq_to_consensus(
        snp_ratios_path=snp_ratios_path,
        snp_freq_path=snp_freq_path,
        reference_path=reference_path,
        out_dir=out_dir,
        min_match_depth=min_match_depth,
    )

def _subtract_indels_from_counts(upper_reads: str, base_counts: Dict[str, int]) -> None:
    for indel in finditer(r'([+|-])(\d+)(\w+)', upper_reads):
        indel_size = int(indel.group(2))
        indel_sequence = indel.group(3)[:indel_size]
        for base in base_counts:
            base_counts[base] -= indel_sequence.count(base)


def _base_ratios_from_reads(reads: str, depth: int, ref: str) -> Dict[str, float]:
    upper_reads = reads.upper()

    # ^G. marks the start of a read segment where the ASCII minus 33 of G is the mapping quality
    # and the character AFTER those 2 is the actual read - in this case a match to the reference.
    # Drop the first 2 characters as they are not actual reads
    if '^' in upper_reads:
        upper_reads = sub('\^.', '', upper_reads)

    base_counts = {base: upper_reads.count(base) for base in 'ACTG'}
    if ref in 'ACTG':
        base_counts[ref] += upper_reads.count('.') + upper_reads.count(',')
    if '+' in upper_reads or '-' in upper_reads:
        _subtract_indels_from_counts(upper_reads, base_counts)
    # Only calculate ratios for present bases
    base_ratios = {
        base: round(base_counts[base] / depth, 3)
        for base in base_counts if base_counts[base]
    }
    return base_ratios


def _pileup_row_to_snp_ratio_row(row: str) -> str:
    contig, pos_str, ref, depth_str, reads, *_ = row.split('\t')
    depth = int(depth_str)
    base_to_ratio = _base_ratios_from_reads(reads, depth, ref)
    # sort by base for backward compatability
    base_ratios = sorted(base_to_ratio.items(),
                         key=lambda base_and_ratio: base_and_ratio[0])
    bases_str = ','.join(base for base, _ in base_ratios)
    ratios_str = ','.join(str(ratio) for _, ratio in base_ratios)
    return '\t'.join((contig, pos_str, ref, depth_str, bases_str, ratios_str))


def pileup_to_snp_ratios(pileup_path: str, snp_ratios_path: str) -> None:
    with file(pileup_path, 'rt') as f_in, file(snp_ratios_path, 'wt') as f_out:
        for row in f_in:
            f_out.write(_pileup_row_to_snp_ratio_row(row) + '\n')

# Determine the genotype at each position and filter by read coverage


def _get_genotype_and_valid_bases_and_valid_ratios(
    ref: str, bases: List[str], ratio_strs: list, hetero_min: float, hetero_max: float
) -> Optional[Tuple[str, str, str]]:

    """Use the heterozygosity thresholds to filter the base ratios and determine the genotype:
    Genotype	Condition	                                        Example
    0/0	        pref ≥ hetero_max or only refbase qualified	        G GG 0/0 1
    0/1	        ref base and 1 alt base qualified	                T CT 0/1 0.217,0.783
    1/1	        palt 1 ≥ hetero_max or only 1 alt base qualified	T CC 1/1 0.822
    1/2	        2 alt bases qualified and ref base did not	        C AT 1/2 0.476,0.524
    ?	        3+ bases qualified	                                T ACT ? 0.2,0.6,0.2
    """

    ratios = map(float, ratio_strs)

    genotype = ''
    valid_bases = ''
    valid_ratios = ''

    for (base, ratio, ratio_str) in zip(bases, ratios, ratio_strs):
        if ratio < hetero_min:
            continue
        valid_ratios += ratio_str + ','
        if ratio >= hetero_max:
            genotype = '0/0' if base == ref else '1/1'
            valid_bases = base + base
            break
        valid_bases += base

    if not valid_bases:
        return None

    if not genotype:
        if len(valid_bases) == 1:
            if valid_bases == ref:  # If only ref has min <= freq <= max
                return None
            genotype = '1/1'
            valid_bases = valid_bases + valid_bases
        elif len(valid_bases) == 2:
            genotype = '0/1' if ref in valid_bases else '1/2'
        else:
            genotype = '?'

    valid_ratios = valid_ratios.rstrip(',')
    return genotype, valid_bases, valid_ratios


def _snp_ratio_row_to_snp_freq_row(
    row: str, min_depth: int, hetero_min: float, hetero_max: float
) -> Optional[str]:

    contig, pos, ref, depth, bases_str, ratios = row.split('\t')

    if not bases_str or int(depth) < min_depth:
        return None

    bases = bases_str.split(',')
    ratio_strs = ratios.rstrip().split(',')

    genotype_and_valid_bases_and_valid_ratios = _get_genotype_and_valid_bases_and_valid_ratios(
        ref, bases, ratio_strs, hetero_min, hetero_max
    )

    if not genotype_and_valid_bases_and_valid_ratios:
        return None

    genotype, valid_bases, valid_ratios = genotype_and_valid_bases_and_valid_ratios

    return '\t'.join((contig, str(int(pos) - 1), pos, ref, depth, valid_bases, genotype, valid_ratios))


def snp_ratios_to_snp_freq(
    snp_ratios_path: str,
    snp_freq_path: str,
    min_depth: int,
    hetero_min: float,
    hetero_max: float,
) -> None:
    with file(snp_ratios_path, 'rt') as f_in, file(snp_freq_path, 'wt') as f_out:
        for row in f_in:
            snp_freq_row = _snp_ratio_row_to_snp_freq_row(
                row, min_depth, hetero_min, hetero_max
            )
            if snp_freq_row:
                f_out.write(snp_freq_row + '\n')


def reads_to_cds_concat(
    fastq: str,
    reference: str,
    gff: str,
    out_dir: str,
    min_snp_depth: int = 20,
    min_match_depth: int = 2,
    hetero_min: float = .25,
    hetero_max: float = .75,
    threads=1,
    trim=True,
    max_read_length: int = 4_000,
) -> str:
    pileup = reads_to_pileup(fastq, reference, out_dir, threads=threads, trim=trim, max_read_length=max_read_length)
    consensus = pileup_to_consensus(
        pileup, reference, out_dir, min_snp_depth,
        min_match_depth, hetero_min, hetero_max
    )
    cds = extract_cds(consensus, gff, out_dir)
    cds_concat = fasta_concat(cds, out_dir)
    return cds_concat


def reads_to_fastqc(fastq: str, out_dir: str) -> Tuple[str, str]:
    makedirs(out_dir, exist_ok=True)
    sample_name, _ = get_sample_name_and_extenstion(fastq, 'fastq')
    fastqc_page = join(out_dir, f'{sample_name}_fastqc.html')
    fastqc_data = join(out_dir, f'{sample_name}_fastqc.zip')
    run(['fastqc', '--quiet', '-o', out_dir, fastq])
    return fastqc_page, fastqc_data


def alignment_to_flagstat(alignment: str, out_dir: str) -> str:
    makedirs(out_dir, exist_ok=True)
    sample_name, _ = get_sample_name_and_extenstion(alignment, 'alignment')
    flagstat = join(out_dir, f'{sample_name}.txt')
    run(['samtools', 'flagstat', alignment], flagstat)
    return flagstat

def seq_to_pct_coverage(seq: str) -> float:
    seq = str(seq).upper()
    n_unknown = seq.count('?') + seq.count('N')
    pct_unknown = 100 * n_unknown / len(seq)
    return 100 - pct_unknown

def consensus_to_coverage(consensus, out_dir, step=1):
    makedirs(out_dir, exist_ok=True)
    sample_name, _ = get_sample_name_and_extenstion(consensus, 'fasta')
    out_path = join(out_dir, f'{sample_name}.csv')
    cov_pcts = [seq_to_pct_coverage(r.seq) for r in parse(consensus, 'fasta')]

    pct_coverage_thresholds = list(range(0, 101, step))
    number_of_genes_with_pct_coverage_ge_thresholds = [
        len([pct for pct in cov_pcts if pct >= min_pct])
        for min_pct in pct_coverage_thresholds
    ]
    pd.DataFrame({
        'pct_exon_positions_covered': pct_coverage_thresholds,
        'number_of_genes': number_of_genes_with_pct_coverage_ge_thresholds,
    }).to_csv(out_path, index=None, header=None)
    return out_path

# This is a work around to ensure that multiqc can create the plot specified in
# config/multiqc_config.yaml.
# Unless there is at least one _mqc.yaml in the directory, multiqc will not 
# pick up any of the custom_data specified in the --config file. To ensure it
# does pick up those files, we create a file for each sample with only id.
def create_empty_config_required_for_gene_coverage_mqc(sample: str, out_dir: str):
    config_name = f'{sample}_empty_config_required_for_gene_coverage_mqc'
    with open(join(out_dir, f'{config_name}.yaml'), 'w') as f:
        f.write(f'id: "{config_name}"' + '\n')


def pileup_to_gene_depths(pileup_path: str, ref_path: str) -> dict:
    depths = {r.id: [0 for _ in r.seq] for r in parse(ref_path, 'fasta')}
    with open(pileup_path) as f:
        for line in f:
            gene, pos, _, depth, *_ = line.split()
            depths[gene][int(pos) - 1] = int(depth)
    return depths
    
def gene_depths_to_amplicon_depths(
    gene_depths: Dict[str, List[int]],
    primers: pd.DataFrame
) -> Dict[str, List[int]]:
    return {
        row['name']: gene_depths[row.gene][row.start:row.end]
        for _, row in primers.iterrows()
    }

def pileup_to_amplicon_depths(pileup_path: str, ref_path: str, primers: pd.DataFrame) -> dict:
    gene_depths = pileup_to_gene_depths(pileup_path, ref_path)
    amplicon_depths = gene_depths_to_amplicon_depths(gene_depths, primers)
    return amplicon_depths

def amplicon_report(
    pileup_path: str,
    ref_path: str,
    primers_path: str,
    out_dir: str,
) -> None:
    sample_name, ext = get_sample_name_and_extenstion(pileup_path, 'pileup')

    primers = pd.read_excel(primers_path)

    amplicon_depths = pileup_to_amplicon_depths(pileup_path, ref_path, primers)

    rows = []
    for amplicon_name, depths in amplicon_depths.items():
        depths = np.array(depths)
        n_exceeding = {
            f'n_ge_{threshold}x': (depths >= threshold).sum()
            for threshold in [1, 2, 5, 10, 20, 50, 100]
        }
        described = pd.Series(depths).describe().drop('count').to_dict()
        rows.append({
            'name': amplicon_name,
            'amplicon_size': len(depths),
            **n_exceeding,
            **described,
        })
    amplicon_depth_stats = pd.DataFrame(rows)
    amplicon_depth_stats = pd.merge(
        primers,
        amplicon_depth_stats,
        on='name'
    )
    amplicon_depth_stats['pool_name'] = 'Pool ' + amplicon_depth_stats['pool'].astype(str)
    amplicon_depth_stats.to_excel(join(out_dir, f'{sample_name}_amplicon_stats.xlsx'), index=None)
    rows = []
    for threshold in range(201):
        rows.append({
            'min_median_depth': threshold,
            'amplicons': amplicon_depth_stats[amplicon_depth_stats['50%'] >= threshold].shape[0],
        })
    amplicon_median_depth_summary = pd.DataFrame(rows)
    amplicon_median_depth_summary.to_csv(join(out_dir, f'{sample_name}_amplicon_median_depth.csv'), index=None, header=None)

    groupby = amplicon_depth_stats.groupby('pool_name')
    for thresh in [10, 20]:
        col = f'n_ge_{thresh}x'
        pool_summary = pd.DataFrame({
            sample_name: 100 * groupby[col].sum() / groupby.amplicon_size.sum()
        }).T
        pool_summary.rename(columns={col: f'{col} ({thresh}x)' for col in pool_summary}, inplace=True)
        pool_summary.columns.name = None
        pool_summary.index.name = 'Sample Name'
        pool_summary = pool_summary.reset_index()
        pool_summary.insert(1, f'All Pools ({thresh}x)', [100 *amplicon_depth_stats[col].sum() / amplicon_depth_stats.amplicon_size.sum()])
        pool_summary.to_csv(join(out_dir, f'{sample_name}_ge_{thresh}x_pool_summary.csv'), index=None)

def sample_report(sample_dir: str, sample_name: str, ref_path: str, primers_path: str):
    out_dir = join(sample_dir, 'report')
    for fastq_ext in ['.fastq', '.fastq.gz', '.fq', '.fq.gz']:
        fastq_path = join(sample_dir, f'{sample_name}{fastq_ext}')
        if isfile(fastq_path):
            reads_to_fastqc(fastq_path, out_dir)
            break
    # TODO: make this more flexible
    reads_to_fastqc(join(sample_dir, f'{sample_name}.fastq'), out_dir)
    alignment_to_flagstat(join(sample_dir, f'{sample_name}.bam'), out_dir)
    consensus_to_coverage(join(sample_dir, f'{sample_name}.fasta'), out_dir)
    create_empty_config_required_for_gene_coverage_mqc(sample_name, out_dir)
    amplicon_report(
        pileup_path=join(sample_dir, f'{sample_name}.pileup'),
        ref_path=ref_path,
        primers_path=primers_path,
        out_dir=out_dir,
    )


def reads_list_to_cds_concat_with_report(
    fastq_paths: List[str],
    reference: str,
    gff: str,
    out_dirs: List[str],
    multiqc_config: str,
    primers_path: str,
    threads=1,
    trim=True,
    max_read_length: int = 4_000,
):
    for fastq_index, (fastq, out_dir) in enumerate(zip(fastq_paths, out_dirs)):
        sample_name = get_sample_name_and_extenstion(fastq, 'fastq')[0]
        print(f'{fastq_index + 1}/{len(fastq_paths)} {sample_name}:', end=' ', flush=True)
        reads_to_cds_concat(
            fastq=fastq,
            reference=reference,
            gff=gff,
            out_dir=out_dir,
            threads=threads,
            trim=trim,
            max_read_length=max_read_length,
        )
        print('assessing', flush=True)
        sample_report(out_dir, sample_name, reference, primers_path)
    print('Report: compiling')
    report_dirs = [join(out_dir, 'report') for out_dir in out_dirs]
    run(['multiqc', '--config', multiqc_config] + report_dirs, out='/dev/null')

def cds_concat_paths_to_tree_input(
    cds_concat_paths: List[str],
    starting_tree_input: str,
    new_tree_input: str,
) -> str:
    with open(new_tree_input, 'w') as f_out:
        for path in [*cds_concat_paths, starting_tree_input]:
            with file(path) as f_in:
                for line in f_in:
                    f_out.write(line)
    # You already know where to find it, but for consistency
    return new_tree_input

def cds_concat_to_newick(cds_concat: str, out_dir: str, n_threads=1) -> str:
    makedirs(out_dir, exist_ok=True)
    collection_name, _ = get_sample_name_and_extenstion(cds_concat, 'fasta')
    # need absolute path because we're about to run raxml from the output directory
    cds_concat = abspath(cds_concat)

    # output name cannot contain slashes, so run raxml from the output directory
    with pushd(out_dir):
        # make a temporary file to prevent raxml log being written to screen
        with tempfile.TemporaryFile() as log:
            try:
                args = [
                    'raxmlHPC-PTHREADS-SSE3',
                     '-s', cds_concat,
                     '-m', 'GTRGAMMA',
                     '-n', f'{collection_name}.newick',
                     '-p', '100',
                ]
                if n_threads >= 2:
                    args += ['-T', str(n_threads)]
                run(args, out=log)
            except:
                # if raxml failed then raise an exception with the error message
                # which was written to the temporary file (redirected from stdout)
                log.seek(0)
                error = log.read().decode()
                # When run with a fasta file, RAxML reports it cannot be parsed as a phylip.
                # This message can make it harder to find the actual error if RAxML crashes,
                # so supress that message.
                error = error.replace(
                    "\nRAxML can't, parse the alignment file as phylip file \nit will now try to parse it as FASTA file\n\n",
                    ''
                )
                raise Exception(error)

    return join(out_dir, f'RAxML_bestTree.{collection_name}.newick')

def newick_to_imgs(newick_path: str, metadata_path: str, out_dir: str, img_fmt='pdf') -> Dict[str, str]:
    makedirs(out_dir, exist_ok=True)
    collection_name, _ = get_sample_name_and_extenstion(newick_path, 'newick')

    # Load metadata and styles
    tree = Phylo.read(newick_path, 'newick')
    tree.root_at_midpoint()
    tree.ladderize(reverse=True)
    sheets = pd.read_excel(metadata_path, sheet_name=None, engine='openpyxl')
    metadata = sheets['metadata'].fillna('?').astype(
        str).set_index('tree_name').to_dict()
    style = {}
    for sheet_name, sheet in sheets.items():
        if sheet_name == 'metadata':
            continue
        style[sheet_name] = sheet.set_index(
            sheet_name)[['color', 'marker']].to_dict(orient='index')

    show_labels = True
    n_leaves = len(tree.get_terminals())
    label_col = 'tree_new_name'

    img_out_paths = {}

    for style_col in list(style) + ['region']:

        img_out_path = join(out_dir, f'{collection_name}_{style_col}.{img_fmt}')
        img_out_paths[style_col] = img_out_path

        height = max(5, n_leaves / 6)
        width = 14
        fontsize = 11 - width / 5
        fig, ax = plt.subplots(1, 1, figsize=(width, height))
        ymin, ymax = (0, n_leaves)
        ax.set_ylim(ymin, ymax)

        Phylo.draw(tree, axes=ax, do_show=False)

        xmin, xmax = ax.get_xlim()

        # Get dimensions of axis in pixels
        x1, x2 = ax.get_window_extent().get_points()[:, 0]
        # Get unit scale
        xscale = (x2 - x1) / (xmax-xmin)
        # Get width of font in data units
        font_size_x_units = fontsize / xscale

        seen_style_vals = set()

        name_to_pos = {}
        name_to_style = {}

        # Update the markers and labels
        texts = [t for t in ax.texts]
        for t in texts:
            s = t.get_text().strip()

            style_val = metadata[style_col].get(s, '?')
            seen_style_vals.add(style_val)

            default_style = {'color': string_to_color(
                style_val), 'marker': '●'}

            if (style_val == '?') or (style_col not in style):
                val_style = default_style
            else:
                val_style = style[style_col].get(style_val, default_style)

            name_to_pos[s] = t.get_position()
            name_to_style[s] = val_style

            color = val_style['color']
            marker = val_style['marker']

            t.set_text(marker)
            t.set_color(color)

            t.set_size(fontsize * 1.2)
            x, y = t.get_position()
            if show_labels:
                s = metadata[label_col].get(s, s)
                ax.text(x + 1.8 * font_size_x_units, y,
                        s, va='center', fontsize=fontsize)

        # Fill in the contiguous regions where the style_val is the same
        right = xmax + .15 * (xmax - xmin)
        text_right = xmax + .142 * (xmax - xmin)
        polygons = []
        to_fill = pd.DataFrame(name_to_pos).T.rename(columns={0: 'x', 1: 'y'})
        to_fill = to_fill.join(pd.DataFrame(name_to_style).T)
        to_fill['style_val'] = [metadata[style_col].get(
            s, '?') for s in to_fill.index]
        to_fill = to_fill.sort_values(by='y')
        poly_x = []
        poly_y: list = []
        curr_style_val = list(to_fill.style_val)[0]
        curr_color = list(to_fill.color)[0]
        for x, y, color, style_val in to_fill[['x', 'y', 'color', 'style_val']].values:
            if style_val != curr_style_val and poly_y:
                poly_x += [right, right]
                poly_y += [poly_y[-1], poly_y[0]]
                polygons += [poly_x, poly_y, curr_color]
                poly_x = []
                poly_y = []
                curr_style_val = style_val
                curr_color = color
            poly_x += [x + 1.2 * font_size_x_units] * 2
            poly_y += [y - .5, y + .5]
        poly_x += [right, right]
        poly_y += [y + .5, poly_y[0]]
        polygons += [poly_x, poly_y, curr_color]
        ax.fill(*polygons, alpha=.15, ec='#555', lw=.25)

        # Add a label to each filled region
        style_val_index = 0
        curr_style_val = None
        style_val_indices = []
        for style_val in to_fill.style_val:
            if style_val != curr_style_val:
                style_val_index += 1
                curr_style_val = style_val
            style_val_indices.append(style_val_index)
        to_fill['style_val_index'] = style_val_indices
        mean_contiguous_y = to_fill.groupby(
            ['style_val_index', 'style_val', 'color']).y.mean().to_frame().reset_index()
        for style_val, y, color in mean_contiguous_y[['style_val', 'y', 'color']].values:
            ax.text(text_right, y, style_val, color=darken_color(color),
                    ha='right', va='center', alpha=.5, fontsize=fontsize)

        # Make the branches thinner
        for collection in ax.collections:
            if list(collection.get_linewidths()) == [1.5]:
                collection.set_linewidths([0.5])

        # Add a legend
        for style_val in sorted(seen_style_vals):
            default_style = {'color': string_to_color(
                style_val), 'marker': '●'}
            if (style_val == '?') or (style_col not in style):
                val_style = default_style
            else:
                val_style = style[style_col].get(style_val, default_style)
            color = val_style['color']
            scatter_style = {
                '●': {'marker': 'o', 'fc': color, 'ec': color},
                '■': {'marker': 's', 'fc': color, 'ec': color},
                '○': {'marker': 'o', 'fc': '#FFF', 'ec': color},
            }[val_style['marker']]
            ax.scatter([], [], label=style_val, **scatter_style)
        ax.legend(title=' '.join(style_col.split('_')).title(), loc='upper left')

        # Hide the right and top spines
        for side in ['right', 'top']:
            ax.spines[side].set_visible(False)

        # Make the left and top axes less prominent
        faint_color = '#BBB'
        for side in ['left', 'bottom']:
            ax.spines[side].set_color(faint_color)
        ax.tick_params(axis='x', colors=faint_color)
        ax.tick_params(axis='y', colors=faint_color)
        ax.yaxis.label.set_color(faint_color)
        ax.xaxis.label.set_color(faint_color)

        ax.set_ylabel(None)

        ax.set_xlim(xmin, right)

        plt.tight_layout()
        plt.savefig(img_out_path)
        plt.close()

    return img_out_paths

def cds_concat_paths_to_tree_imgs(
    cds_concat_paths: List[str],
    starting_tree_input: str,
    tree_name: str,
    out_dir: str,
    metadata_path: str,
    n_threads=1,
    img_fmt='pdf',
) -> Dict[str, str]:
    makedirs(out_dir, exist_ok=True)
    tree_input = join(out_dir, f'{tree_name}.fasta')
    cds_concat_paths_to_tree_input(
        cds_concat_paths=cds_concat_paths,
        starting_tree_input=starting_tree_input,
        new_tree_input=tree_input,
    )
    print('Tree: making', end=' ', flush=True)
    newick = cds_concat_to_newick(
        cds_concat=tree_input,
        out_dir=out_dir,
        n_threads=n_threads,
    )
    print('visualising', flush=True)
    imgs = newick_to_imgs(
        newick_path=newick,
        metadata_path=metadata_path,
        out_dir=out_dir,
        img_fmt=img_fmt,
    )
    return imgs

