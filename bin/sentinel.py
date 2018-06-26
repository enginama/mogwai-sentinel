#!/usr/bin/env python
import sys
import os
sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), '../lib')))
import init
import config
import misc
from mogwaid import MogwaiDaemon
from models import Superblock, Proposal, GovernanceObject, Watchdog
from models import VoteSignals, VoteOutcomes, Transient
import socket
from misc import printdbg
import time
from bitcoinrpc.authproxy import JSONRPCException
import signal
import atexit
import random
from scheduler import Scheduler
import argparse


# sync mogwaid gobject list with our local relational DB backend
def perform_mogwaid_object_sync(mogwaid):
    GovernanceObject.sync(mogwaid)


# delete old watchdog objects, create new when necessary
def watchdog_check(mogwaid):
    printdbg("in watchdog_check")

    # delete expired watchdogs
    for wd in Watchdog.expired(mogwaid):
        printdbg("\tFound expired watchdog [%s], voting to delete" % wd.object_hash)
        wd.vote(mogwaid, VoteSignals.delete, VoteOutcomes.yes)

    # now, get all the active ones...
    active_wd = Watchdog.active(mogwaid)
    active_count = active_wd.count()

    # none exist, submit a new one to the network
    if 0 == active_count:
        # create/submit one
        printdbg("\tNo watchdogs exist... submitting new one.")
        wd = Watchdog(created_at=int(time.time()))
        wd.submit(mogwaid)

    else:
        wd_list = sorted(active_wd, key=lambda wd: wd.object_hash)

        # highest hash wins
        winner = wd_list.pop()
        printdbg("\tFound winning watchdog [%s], voting VALID" % winner.object_hash)
        winner.vote(mogwaid, VoteSignals.valid, VoteOutcomes.yes)

        # if remaining Watchdogs exist in the list, vote delete
        for wd in wd_list:
            printdbg("\tFound losing watchdog [%s], voting DELETE" % wd.object_hash)
            wd.vote(mogwaid, VoteSignals.delete, VoteOutcomes.yes)

    printdbg("leaving watchdog_check")


def prune_expired_proposals(mogwaid):
    # vote delete for old proposals
    for proposal in Proposal.expired(mogwaid.superblockcycle()):
        proposal.vote(mogwaid, VoteSignals.delete, VoteOutcomes.yes)


# ping mogwaid
def sentinel_ping(mogwaid):
    printdbg("in sentinel_ping")

    mogwaid.ping()

    printdbg("leaving sentinel_ping")


def attempt_superblock_creation(mogwaid):
    import mogwailib

    if not mogwaid.is_masternode():
        print("We are not a Masternode... can't submit superblocks!")
        return

    # query votes for this specific ebh... if we have voted for this specific
    # ebh, then it's voted on. since we track votes this is all done using joins
    # against the votes table
    #
    # has this masternode voted on *any* superblocks at the given event_block_height?
    # have we voted FUNDING=YES for a superblock for this specific event_block_height?

    event_block_height = mogwaid.next_superblock_height()

    if Superblock.is_voted_funding(event_block_height):
        # printdbg("ALREADY VOTED! 'til next time!")

        # vote down any new SBs because we've already chosen a winner
        for sb in Superblock.at_height(event_block_height):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(mogwaid, VoteSignals.funding, VoteOutcomes.no)

        # now return, we're done
        return

    if not mogwaid.is_govobj_maturity_phase():
        printdbg("Not in maturity phase yet -- will not attempt Superblock")
        return

    proposals = Proposal.approved_and_ranked(proposal_quorum=mogwaid.governance_quorum(), next_superblock_max_budget=mogwaid.next_superblock_max_budget())
    budget_max = mogwaid.get_superblock_budget_allocation(event_block_height)
    sb_epoch_time = mogwaid.block_height_to_epoch(event_block_height)

    sb = mogwailib.create_superblock(proposals, event_block_height, budget_max, sb_epoch_time)
    if not sb:
        printdbg("No superblock created, sorry. Returning.")
        return

    # find the deterministic SB w/highest object_hash in the DB
    dbrec = Superblock.find_highest_deterministic(sb.hex_hash())
    if dbrec:
        dbrec.vote(mogwaid, VoteSignals.funding, VoteOutcomes.yes)

        # any other blocks which match the sb_hash are duplicates, delete them
        for sb in Superblock.select().where(Superblock.sb_hash == sb.hex_hash()):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(mogwaid, VoteSignals.delete, VoteOutcomes.yes)

        printdbg("VOTED FUNDING FOR SB! We're done here 'til next superblock cycle.")
        return
    else:
        printdbg("The correct superblock wasn't found on the network...")

    # if we are the elected masternode...
    if (mogwaid.we_are_the_winner()):
        printdbg("we are the winner! Submit SB to network")
        sb.submit(mogwaid)


def check_object_validity(mogwaid):
    # vote (in)valid objects
    for gov_class in [Proposal, Superblock]:
        for obj in gov_class.select():
            obj.vote_validity(mogwaid)


def is_mogwaid_port_open(mogwaid):
    # test socket open before beginning, display instructive message to MN
    # operators if it's not
    port_open = False
    try:
        info = mogwaid.rpc_command('getgovernanceinfo')
        port_open = True
    except (socket.error, JSONRPCException) as e:
        print("%s" % e)

    return port_open


def main():
    mogwaid = MogwaiDaemon.from_mogwai_conf(config.mogwai_conf)
    options = process_args()

    # check mogwaid connectivity
    if not is_mogwaid_port_open(mogwaid):
        print("Cannot connect to mogwaid. Please ensure mogwaid is running and the JSONRPC port is open to Sentinel.")
        return

    # check mogwaid sync
    if not mogwaid.is_synced():
        print("mogwaid not synced with network! Awaiting full sync before running Sentinel.")
        return

    # ensure valid masternode
    if not mogwaid.is_masternode():
        print("Invalid Masternode Status, cannot continue.")
        return

    # register a handler if SENTINEL_DEBUG is set
    if os.environ.get('SENTINEL_DEBUG', None):
        import logging
        logger = logging.getLogger('peewee')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(logging.StreamHandler())

    if options.bypass:
        # bypassing scheduler, remove the scheduled event
        printdbg("--bypass-schedule option used, clearing schedule")
        Scheduler.clear_schedule()

    if not Scheduler.is_run_time():
        printdbg("Not yet time for an object sync/vote, moving on.")
        return

    if not options.bypass:
        # delay to account for cron minute sync
        Scheduler.delay()

    # running now, so remove the scheduled event
    Scheduler.clear_schedule()

    # ========================================================================
    # general flow:
    # ========================================================================
    #
    # load "gobject list" rpc command data, sync objects into internal database
    perform_mogwaid_object_sync(mogwaid)

    if mogwaid.has_sentinel_ping:
        sentinel_ping(mogwaid)
    else:
        # delete old watchdog objects, create a new if necessary
        watchdog_check(mogwaid)

    # auto vote network objects as valid/invalid
    # check_object_validity(mogwaid)

    # vote to delete expired proposals
    prune_expired_proposals(mogwaid)

    # create a Superblock if necessary
    attempt_superblock_creation(mogwaid)

    # schedule the next run
    Scheduler.schedule_next_run()


def signal_handler(signum, frame):
    print("Got a signal [%d], cleaning up..." % (signum))
    Transient.delete('SENTINEL_RUNNING')
    sys.exit(1)


def cleanup():
    Transient.delete(mutex_key)


def process_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--bypass-scheduler',
                        action='store_true',
                        help='Bypass scheduler and sync/vote immediately',
                        dest='bypass')
    args = parser.parse_args()

    return args


if __name__ == '__main__':
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)

    # ensure another instance of Sentinel is not currently running
    mutex_key = 'SENTINEL_RUNNING'
    # assume that all processes expire after 'timeout_seconds' seconds
    timeout_seconds = 90

    is_running = Transient.get(mutex_key)
    if is_running:
        printdbg("An instance of Sentinel is already running -- aborting.")
        sys.exit(1)
    else:
        Transient.set(mutex_key, misc.now(), timeout_seconds)

    # locked to this instance -- perform main logic here
    main()

    Transient.delete(mutex_key)
