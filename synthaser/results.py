#!/usr/bin/env python3

"""

"""


import logging
import json
import re

from collections import defaultdict
from operator import attrgetter

from synthaser import settings
from synthaser.models import Domain


LOG = logging.getLogger(__name__)


DOMAINS = {}


def load_domain_json(json_file):
    with open(json_file) as fp:
        rules = json.load(fp)
        update_domains(rules)


def update_domains(new):
    DOMAINS.clear()
    DOMAINS.update(new)


# Load defaults, stored in synthaser/domains.json
load_domain_json(settings.DOMAIN_FILE)


def domain_from_row(row):
    """Parse a domain hit from a row in a CD-search results file.

    For example, a typical row might looks like:

    >>> print(row)
    Q#1 - >AN6791.2\tspecific\t225858\t9\t1134\t0\t696.51\tCOG3321\tPksD\t-\tcl09938

    Using this function will generate:

    >>> domain_from_row(row)
    PksD [KS] 9-1134

    Parameters:
        row (str): Tab-separated row from a CDSearch results file
    Returns:
        Domain: Instance of the Domain class containing information about this hit
    Raises:
        ValueError: If the domain in this row is not in the DOMAINS dictionary.
    """
    (
        *_,
        pssm,
        start,
        end,
        evalue,
        bitscore,
        accession,
        domain,
        partial,
        superfamily,
    ) = row.split("\t")

    if domain not in DOMAINS:
        raise ValueError(f"'{domain}' not a synthaser key domain")

    return Domain(
        pssm=pssm,
        type=DOMAINS[domain]["type"],
        domain=domain,
        start=int(start),
        end=int(end),
        evalue=float(evalue),
        bitscore=float(bitscore),
        partial=partial,
        accession=accession,
        superfamily=superfamily,
    )


def parse_rpsbproc(handle):
    """Parse a results file generated by rpsblast->rpsbproc.

    This function takes a handle corresponding to a rpsbproc output file.
    local.rpsbproc returns a subprocess.CompletedProcess object, which contains the
    results as byte string in it's stdout attribute.
    """
    # Sanitize input. Should work for either an open file handle (str, still contains \n
    # when iterating) or byte-string stdout stored in a CompletedProcess object passed to this
    # function as e.g. process.stdout.splitlines()
    stdout = "\n".join(
        line.decode().strip() if isinstance(line, bytes) else line.strip()
        for line in handle
    )

    # Files produced by rpsbproc have anchors for easy parsing. Each query sequence
    # is given a block starting/ending with QUERY/ENDQUERY, and domain hits for the
    # query with DOMAINS/ENDDOMAINS.
    query_pattern = re.compile(
        r"QUERY\tQuery_\d+\tPeptide\t\d+\t([A-Za-z0-9.]+?)\n"
        r"DOMAINS\n(.+?)ENDDOMAINS",
        re.DOTALL,
    )

    domains = defaultdict(list)
    for match in query_pattern.finditer(stdout):
        query = match.group(1)
        for row in match.group(2).split("\n"):
            try:
                domain = domain_from_row(row)
            except ValueError:
                continue
            domains[query].append(domain)

    return domains


def parse_cdsearch(handle):
    """Parse a CD-Search results table and instantiate Domain objects for each hit.

    Parameters:
        handle (file): Open file handle corresponding to a CD-Search results file.
    Returns:
        results (dict): Lists of Domain objects keyed on the query they were found in.
    """
    query_regex = re.compile(r"Q#\d+? - [>]?(.+?)\t")
    results = defaultdict(list)
    for row in handle:
        try:
            row = row.decode()
        except AttributeError:
            pass  # in case rows are unicode
        if not row.startswith("Q#") or row.isspace():
            continue
        query = query_regex.search(row).group(1)
        try:
            domain = domain_from_row(row)
        except ValueError:
            continue
        results[query].append(domain)
    return dict(results)


def filter_results(results, **kwargs):
    """Build Synthase objects from a parsed results dictionary.

    Any additional kwargs are passed to _filter_domains.

    Parameters:
        results (dict): Grouped Domains; output from _parse_cdsearch_table.
    Returns:
        synthases (list): Synthase objects containing all Domain objects found in the CD-Search.
    """
    filtered = {}
    for name, domains in results.items():
        domains = filter_domains(domains, **kwargs)
        if not domains:
            LOG.error("No domains remain after filtering for %s", name)
        filtered[name] = domains
    return filtered


def is_fragmented_domain(one, two, coverage_pct=0.5, tolerance_pct=0.1):
    """Detect if two adjacent domains are likely a single domain.

    This is useful in cases where a domain is detected with multiple small hits. For
    example, an NRPS may have two adjacent condensation (C) domain hits that are
    both individually too small and low-scoring, but should likely just be merged.

    If two hits are close enough together, such that the distance between the start
    of the first and end of the second is within some tolerance (default +-10%) of the
    total length of a domains PSSM, this function will return True.

    Parameters:
        one (Domain): Domain instance
        two (Domain): Domain instance
        coverage_pct (float):
            Conserved domain hit percentage coverage threshold. A hit is considered
            truncated if its total length is less than coverage_pct * CD length.
        tolerance_pct (float):
            Percentage of CD length to use when calculating acceptable lower/upper
            bounds for combined domains.
    Returns:
        True: Domain instances are likely fragmented and should be combined.
        False: Domain instances should be separate.
    """
    if one.type != two.type:
        raise ValueError("Expected Domain instances of same type")

    pssm_length = DOMAINS[one.domain]["length"]
    coverage = pssm_length * coverage_pct
    tolerance = pssm_length * tolerance_pct
    one_length, two_length = len(one), len(two)

    return (
        one_length < coverage
        and two_length < coverage
        and pssm_length - tolerance <= two.end - one.start <= pssm_length + tolerance
        and one_length + two_length > coverage
    )


def filter_domains(domains, by="evalue", coverage_pct=0.5, tolerance_pct=0.1):
    """Filter overlapping Domain objects and test adjcency rules.

    Adjacency rules are tested again here, in case they are missed within overlap
    groups. For example, the NRPS-para261 domain is not always entirely contained by
    a condensation domain, so should be caught by this pass.

    Parameters:
        domains (list): Domain instances to be filtered
        by (str): Metric used to choose representative domain hit (def. 'evalue')
        coverage_pct (float): Conserved domain coverage percentage threshold
        tolerance_pct (float): CD length tolerance percentage threshold
    Returns:
        list: Domain objects remaining after filtering
    """

    domains = [
        choose_representative_domain(group, by)
        for group in group_overlapping_hits(domains)
    ]

    i, total = 1, len(domains)
    while i < total:
        if i + 1 == total:
            break
        previous, current = domains[i - 1 : i + 1]

        # When domains are likely together, e.g. two small C domain hits right next
        # to each other or multiple Methyltransf_X domains, extend its border
        if previous.type == current.type and is_fragmented_domain(
            previous, current, coverage_pct, tolerance_pct
        ):
            previous.end = current.end
            del domains[i]
            continue
        i += 1
    return domains


def choose_representative_domain(group, by="evalue"):
    """Select the best domain from a collection of overlapping domains.

    This function tests rules stored in `special_rules`, which are lambdas that
    take two variables. It sorts the group by e-value, then tests each rule using
    the container (first, best scoring group) against all other Domains in the
    group.

    If any test is True, the container type is set to the rule key and returned.
    Otherwise, this function will return the container Domain with no modification.

    Parameters:
        group (list): Overlapping Domain objects
        by (str):
            Measure to use when determining the best domain of the group. Choices:
            'bitscore': return domain with highest bitscore (relative to threshold)
            'evalue': return domain with lowest E-value
            'length': return longest domain hit
    Returns:
        Domain:
            Highest scoring Domain in the group. If any special rules have been
            satisfied, the type of this Domain will be set to that rule
            (e.g. Condensation -> Epimerization).
    """
    key_functions = {
        "bitscore": (lambda d: d.bitscore / DOMAINS[d.domain]["bitscore"], True),
        "evalue": (lambda d: d.evalue, False),
        "length": (lambda d: d.end - d.start, True),
    }

    if by not in key_functions:
        raise ValueError("Expected 'bitscore', 'evalue' or 'length'")

    key, reverse = key_functions[by]

    return sorted(group, key=key, reverse=reverse)[0]


def group_overlapping_hits(domains):
    """Iterator that groups Domain objects based on overlapping locations.

    Parameters:
        domains (list): Collection of Domain objects belonging to a Synthase
    Yields:
        group (list): Group of overlapping Domain objects
    """
    sorted_domains = sorted(domains, key=attrgetter("start"))

    if not sorted_domains:
        return

    # Initialise first group and initial upper bound
    first = sorted_domains.pop(0)
    group, border = [first], first.end

    for domain in sorted_domains:

        # New domain overlaps current run, so save and set new upper bound
        # Use 10bp to account for slight domain overlap between distinct groups
        if domain.start + 10 <= border:
            group.append(domain)
            border = max(border, domain.end)

        # Current run is over; yield and reset
        else:
            yield group
            group, border = [domain], domain.end

    # End the final run
    yield group


def parse(handle, mode="remote", **kwargs):
    """Parse CD-Search results.

    Any additional kwargs are passed to `synthases_from_results`.

    Parameters:
        handle (file):
            An open CD-Search results file handle. If you used the website to
            analyse your sequences, the file you should download is Domain hits,
            Data mode: Full, ASN text. When using a `CDSearch` object, this
            format is automatically selected.
        mode (str): Search mode ('local' or 'remote')
    Returns:
        list: A list of Synthase objects parsed from the results file.
    Raises:
        ValueError: Search mode not 'local' or 'remote'
    """
    if mode == "remote":
        return filter_results(parse_cdsearch(handle), **kwargs)
    if mode == "local":
        return filter_results(parse_rpsbproc(handle), **kwargs)
    raise ValueError("Expected 'remote' or 'local'")
