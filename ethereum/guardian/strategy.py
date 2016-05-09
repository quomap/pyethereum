import sys
import random

import rlp

from ethereum.ecdsa_accounts import (
    sign_block,
    privtoaddr,
    sign_bet,
    mk_transaction,
    mk_validation_code,
)
from ethereum.utils import (
    big_endian_to_int,
    encode_int,
    shardify,
    DEBUG,
    rlp_decode,
    sha3,
    address,
)
from ethereum.abi import decode_abi
from ethereum.config import (
    GENESIS_TIME,
    BLKTIME,
    ENTER_EXIT_DELAY,
    ADDR_BYTES,
    VALIDATOR_ROUNDS,
    LOG,
    CASPER,
    GASLIMIT,
    ETHER,
    NONCE,
)
from ethereum.serenity_blocks import (
    BLKNUMBER,
    State,
    Block,
    Transaction,
    tx_state_transition,
    block_state_transition,
    get_code,
)
from ethereum.default_betting_strategy import bet_at_height
from ethereum.mandatory_account_code import mandatory_account_evm

from ethereum.guardian.network import (
    NetworkMessage,
    NM_LIST,
    NM_BLOCK,
    NM_BET,
    NM_BET_REQUEST,
    NM_TRANSACTION,
    NM_GETBLOCK,
    NM_GETBLOCKS,
    NM_FAUCET,
)
from ethereum.guardian.utils import (
    casper_ct,
    call_casper,
    is_block_valid,
    encode_prob,
    decode_prob,
    FINALITY_HIGH,
    FINALITY_LOW,
    get_guardian_index,
)


MAX_RECALC = 9
MAX_LONG_RECALC = 14


# An object that stores a bet made by a guardian
class Bet():
    def __init__(self, index, max_height, probs, blockhashes, stateroots, stateroot_probs, prevhash, seq, sig):
        self.index = index
        self.max_height = max_height
        self.probs = probs
        self.blockhashes = blockhashes
        self.stateroots = stateroots
        self.stateroot_probs = stateroot_probs
        self.prevhash = prevhash
        self.seq = seq
        self.sig = sig
        self._hash = None

    # Serializes the bet into the function message which can be directly submitted
    # to the casper contract
    def serialize(self):
        o = casper_ct.encode(
            'submitBet',
            [self.index, self.max_height, ''.join(map(encode_prob, self.probs)),
             self.blockhashes, self.stateroots, ''.join(map(encode_prob, self.stateroot_probs)),
             self.prevhash, self.seq, self.sig]
        )
        self._hash = sha3(o)
        return o

    # Inverse of serialization
    @classmethod
    def deserialize(self, betdata):
        params = decode_abi(casper_ct.function_data['submitBet']['encode_types'],
                            betdata[4:])
        o = Bet(params[0], params[1], map(decode_prob, params[2]), params[3],
                params[4], map(decode_prob, params[5]), params[6], params[7], params[8])
        o._hash = sha3(betdata)
        return o

    # Warning: edit bets very carefully! Make sure hash is always correct
    @property
    def hash(self, recompute=False):
        if not self._hash or recompute:
            self._hash = sha3(self.serialize())
        return self._hash


# An object that stores the "current opinion" of a guardian, as computed
# from their chain of bets
class Opinion():
    def __init__(self, validation_code, index, prevhash, seq, induction_height):
        self.validation_code = validation_code
        self.index = index
        self.blockhashes = []
        self.stateroots = []
        self.probs = []
        self.stateroot_probs = []
        self.prevhash = prevhash
        self.seq = seq
        self.induction_height = induction_height
        self.withdrawal_height = 2**100
        self.withdrawn = False

    def process_bet(self, bet):
        # TODO: check crypto
        if bet.seq != self.seq:
            sys.stderr.write('Bet sequence number does not match expectation: actual %d desired %d\n' % (bet.seq, self.seq))
            return False
        if bet.prevhash != self.prevhash:
            sys.stderr.write('Bet hash does not match prevhash: actual %s desired %s. Seq: %d \n' %
                             (bet.prevhash.encode('hex'), self.prevhash.encode('hex'), bet.seq))
        if self.withdrawn:
            raise Exception("Bet made after withdrawal! Slashing condition triggered!")
        # Update seq and hash
        self.seq = bet.seq + 1
        self.prevhash = bet.hash
        # A bet with max height 2**256 - 1 signals withdrawal
        if bet.max_height == 2**256 - 1:
            self.withdrawn = True
            self.withdrawal_height = self.max_height
            DEBUG("Guardian leaving!", index=bet.index)
            return True
        # Extend probs, blockhashes and state roots arrays as needed
        while len(self.probs) <= bet.max_height:
            self.probs.append(None)
            self.blockhashes.append(None)
            self.stateroots.append(None)
            self.stateroot_probs.append(None)
        # Update probabilities, blockhashes and stateroots
        for i in range(len(bet.probs)):
            self.probs[bet.max_height - i] = bet.probs[i]
        for i in range(len(bet.blockhashes)):
            self.blockhashes[bet.max_height - i] = bet.blockhashes[i]
        for i in range(len(bet.stateroots)):
            self.stateroots[bet.max_height - i] = bet.stateroots[i]
        for i in range(len(bet.stateroot_probs)):
            self.stateroot_probs[bet.max_height - i] = bet.stateroot_probs[i]
        return True

    def get_prob(self, h):
        return self.probs[h] if h < len(self.probs) else None

    def get_blockhash(self, h):
        return self.blockhashes[h] if h < len(self.probs) else None

    def get_stateroot(self, h):
        return self.stateroots[h] if h < len(self.probs) else None

    @property
    def max_height(self):
        return len(self.probs) - 1


class RLPNone(object):
    def serialize(self, obj):
        if obj is not None:
            raise rlp.SerializationError("Cannot serialize non-None types", obj)
        return b'\00'

    def deserialize(self, serial):
        if serial != b'\00':
            raise rlp.DeserializationError("Can only deserialize null byte", serial)
        return None


rlp_none = RLPNone()


class RLPFloat(object):
    def serialize(self, obj):
        if not isinstance(obj, float):
            raise rlp.SerializationError("Cannot serialize float types", obj)
        return rlp.encode(obj.hex())

    def deserialize(self, serial):
        return float.fromhex(rlp.decode(serial))


rlp_float = RLPFloat()


class Empty(object):
    pass


empty = Empty()


class LDBValue(object):
    key = None
    db = None

    def __init__(self, key, default=empty, sedes=None):
        self.default = default
        self.key = rlp.encode(key)
        self.sedes = sedes

    def __get__(self, instance, cls=None):
        try:
            db_value = instance.db.db.Get(self.key)
        except KeyError:
            if self.default is not empty:
                return self.default
            raise

        return rlp.decode(db_value, self.sedes)

    def __set__(self, instance, value):
        if value is None:
            db_value = rlp.encode(value, rlp_none)
        else:
            db_value = rlp.encode(value, self.sedes)
        instance.db.db.Put(self.key, db_value)

    def __delete__(self, instance):
        instance.db.db.Delete(self.key)


class LDBBase(object):
    db = None
    sedes = None

    def __init__(self, db, ns, sedes=None):
        self.db = db
        self.ns = ns
        self.sedes = sedes

    def _to_db_key(self, key):
        return "{0}:{1}".format(self.ns, rlp.encode(key))

    def _set_length(self, length):
        db_key = self._to_db_key("__len__")
        db_value = rlp.encode(length, rlp.sedes.big_endian_int)
        self.db.Put(db_key, db_value)

    def __len__(self):
        db_key = self._to_db_key("__len__")
        try:
            db_value = self.db.Get(db_key)
        except KeyError:
            return 0
        return rlp.decode(db_value, rlp.sedes.big_endian_int)


class LDBList(LDBBase):
    def __iter__(self):
        for idx in range(len(self)):
            yield self[idx]
        raise StopIteration

    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start or 0
            step = key.step or 1
            stop = min(key.stop or len(self), len(self))
            return [self[k] for k in range(start, stop, step)]
        elif key < -1 * len(self) or key >= len(self):
            raise IndexError("list index out of range")
        elif key < 0:
            key = key % len(self)
        db_key = self._to_db_key(key)
        db_value = self.db.Get(db_key)
        try:
            return rlp.decode(db_value, rlp_none)
        except rlp.DeserializationError:
            return rlp.decode(db_value, self.sedes)

    def __setitem__(self, key, value):
        if key < -1 * len(self) or key >= len(self):
            raise IndexError("list index out of range")
        elif key < 0:
            key = key % len(self)
        db_key = self._to_db_key(key)
        if value is None:
            db_value = rlp.encode(value, rlp_none)
        else:
            db_value = rlp.encode(value, self.sedes)
        return self.db.Put(db_key, db_value)

    def append(self, value):
        idx = len(self)
        self._set_length(len(self) + 1)
        self[idx] = value

    def __contains__(self, value):
        for v in self:
            if v == value:
                return True
        return False


class LDBDict(LDBBase):
    def __init__(self, *args, **kwargs):
        super(LDBDict, self).__init__(*args, **kwargs)
        key_ns = self._to_db_key('__keys__')
        self._keys = LDBList(self.db, key_ns)

    def __contains__(self, key):
        db_key = self._to_db_key(key)
        try:
            self.db.Get(db_key)
            return True
        except KeyError:
            return False

    def __getitem__(self, key):
        db_key = self._to_db_key(key)
        db_value = self.db.Get(db_key)
        return rlp.decode(db_value, self.sedes)

    def __setitem__(self, key, value):
        db_key = self._to_db_key(key)

        if value is None:
            db_value = rlp.encode(value, rlp_none)
        else:
            db_value = rlp.encode(value, self.sedes)

        is_new_key = key not in self

        self.db.Put(db_key, db_value)

        if is_new_key:
            self._set_length(len(self) + 1)
            self._keys.append(key)

    def __delitem__(self, key):
        db_key = self._to_db_key(key)
        self.db.Delete(db_key)
        self._set_length(len(self) - 1)

        keys = [k for k in self.keys() if k != key]
        assert len(keys) == len(self)
        for idx, k in enumerate(keys):
            self._keys[idx] = k

        self._keys._set_length(len(keys))

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return list(self._keys)

    def values(self):
        return [self[key] for key in self.keys()]

    def items(self):
        return [(key, self[key]) for key in self.keys()]


# The default betting strategy; initialize with the genesis block and a privkey
class defaultBetStrategy():
    @property
    def id(self):
        from ethereum.utils import decode_int
        return decode_int(self.addr[2:])

    last_bet_made = LDBValue('last_bet_made', 0, rlp.sedes.big_endian_int)
    last_time_sent_getblocks = LDBValue('last_time_sent_getblocks', 0, rlp.sedes.big_endian_int)
    join_at_block = LDBValue('join_at_block', -1, rlp.sedes.big_endian_int)
    last_faucet_request = LDBValue('last_faucet_request', -1, rlp.sedes.big_endian_int)
    index = LDBValue('index', -1, rlp.sedes.big_endian_int)
    former_index = LDBValue('former_index', None, rlp.sedes.big_endian_int)
    last_block_produced = LDBValue('last_block_produced', -1, rlp.sedes.big_endian_int)
    next_block_to_produce = LDBValue('next_block_to_produce', -1, rlp.sedes.big_endian_int)
    prevhash = LDBValue('prevhash', '\x00' * 32)
    seq = LDBValue('seq', 0, rlp.sedes.big_endian_int)
    calc_state_roots_from = LDBValue('calc_state_roots_from', 0, rlp.sedes.big_endian_int)
    max_finalized_height = LDBValue('max_finalized_height', -1, rlp.sedes.big_endian_int)

    def __init__(self, genesis_state, key, clockwrong=False, bravery=0.92,
                 crazy_bet=False, double_block_suicide=2**200,
                 double_bet_suicide=2**200, min_gas_price=10**9, join_at_block=None):
        DEBUG("Initializing betting strategy")
        # Guardian's private key
        self.key = key
        # Guardian's address on the network
        self.addr = privtoaddr(key)
        # The bet strategy's database
        self.db = genesis_state.db
        # This counter is incremented every time a guardian joins;
        # it allows us to re-process the guardian set and refresh
        # the guardians that we have
        self.guardian_signups = call_casper(genesis_state, 'getGuardianSignups', [])
        # A dict of opinion objects containing the current opinions of all
        # guardians
        self.opinions = {}
        # A dict of lists of bets received from guardians
        self.bets = {}
        # The probabilities that you are betting
        self.probs = LDBList(self.db.db, 'probs', rlp_float)
        # Your finalized block hashes
        self.finalized_hashes = LDBList(self.db.db, 'finalized_hashes')
        # Your state roots
        self.stateroots = LDBList(self.db.db, 'stateroots')
        # Which counters have been processed
        self.counters = {}
        # A dict containing the highest-sequence-number bet processed for
        # each guardian
        self.highest_bet_processed = {}
        # The time when you received an object
        self.time_received = {}
        # Hash lookup map; used mainly to check whether or not something has
        # already been received and processed
        self.objects = {}
        # Blocks selected for each height
        self.blocks = LDBList(self.db.db, 'blocks', Block)
        # When you last explicitly requested to ask for a block; stored to
        # prevent excessively frequent lookups
        self.last_asked_for_block = LDBDict(self.db.db, 'last_asked_for_block', rlp_float)
        # When you last explicitly requested to ask for bets from a given
        # guardian; stored to prevent excessively frequent lookups
        self.last_asked_for_bets = LDBDict(self.db.db, 'last_asked_for_bets', rlp_float)
        # Pool of transactions worth including
        self.txpool = LDBDict(self.db.db, 'txpool', Transaction)
        # Map of hash -> (tx, [(blknum, index), ...]) for transactions that
        # are in blocks that are not fully confirmed
        self.unconfirmed_txindex = {}
        # Map of hash -> (tx, [(blknum, index), ...]) for transactions that
        # are in blocks that are fully confirmed
        self.finalized_txindex = {}
        # Counter for number of times a transaction entered an exceptional
        # condition
        self.tx_exceptions = LDBDict(self.db.db, 'tx_exceptions', rlp.sedes.big_endian_int)
        # Last time you made a bet; stored to prevent excessively
        # frequent betting
        #self.last_bet_made = 0
        # Last time sent a getblocks message; stored to prevent excessively
        # frequent getting
        #self.last_time_sent_getblocks = 0
        # The block that this guardian should try to join the pool
        if join_at_block is not None:
            self.join_at_block = join_at_block
        # Last time we requested ether
        #self.last_faucet_request = -1
        # Your guardian index
        #self.index = -1
        #self.former_index = None
        # Store the genesis block state here
        self.genesis_state_root = genesis_state.root
        # Store the timestamp of the genesis block
        self.genesis_time = big_endian_to_int(genesis_state.get_storage(GENESIS_TIME, '\x00' * 32))
        # Last block that you produced
        #self.last_block_produced = -1
        # Next height at which you are eligible to produce (could be None)
        #self.next_block_to_produce = -1
        # Deliberately sabotage my clock? (for testing purposes)
        self.clockwrong = clockwrong
        # How quickly to converge toward finalization?
        self.bravery = bravery
        assert 0 < self.bravery <= 1
        # Am I making crazy bets? (for testing purposes)
        self.crazy_bet = crazy_bet
        # What block number to create two blocks at, destroying my guardian
        # slot (for testing purposes; for non-byzantine nodes set to some really
        # high number)
        self.double_block_suicide = double_block_suicide
        # What seq to create two bets at (also destructively, for testing purposes)
        self.double_bet_suicide = double_bet_suicide
        # List of proposers for blocks; calculated into the future just-in-time
        self.proposers = LDBList(self.db.db, 'proposers', rlp.sedes.big_endian_int)
        # Prevhash (for betting)
        #self.prevhash = '\x00' * 32
        # Sequence number (for betting)
        #self.seq = 0
        # Transactions I want to track
        self.tracked_tx_hashes = LDBList(self.db.db, 'tracked_tx_hashes')
        # If we only partially calculate state roots, store the index at which
        # to start calculating next time you make a bet
        #self.calc_state_roots_from = 0
        # Minimum gas price that I accept
        self.min_gas_price = min_gas_price
        # Max height which is finalized from your point of view
        #self.max_finalized_height = -1
        # Create my guardian set
        self.update_guardian_set(self.get_finalized_state())
        DEBUG('Found %d guardians in genesis' % len(self.opinions))
        # The height at which this guardian is added
        self.induction_height = call_casper(genesis_state, 'getGuardianInductionHeight', [self.index]) if self.index >= 0 else 2**100
        DEBUG("Initialized guardian",
              address=self.addr.encode('hex'),
              index=self.index,
              induction_height=self.induction_height)
        self.withdrawn = False
        # Recently discovered blocks
        self.recently_discovered_blocks = LDBList(self.db.db, 'recently_discovered_blocks', rlp.sedes.big_endian_int)
        # When will I suicide?
        if self.double_block_suicide < 2**40:
            if self.double_block_suicide < self.next_block_to_produce:
                DEBUG("Suiciding at block %d" % self.next_block_to_produce)
            else:
                DEBUG("Suiciding at some block after %d" % self.double_block_suicide)
        DEBUG('List of', proposers=self.proposers)
        # Am I byzantine?
        self.byzantine = self.crazy_bet or self.double_block_suicide < 2**80 or self.double_bet_suicide < 2**80

    _joined_at_block = LDBValue('_joined_at_block', -1, rlp.sedes.big_endian_int)

    _last_nonce = LDBValue('_last_nonce', -1, rlp.sedes.big_endian_int)

    def get_nonce(self, optimistic=True):
        if optimistic:
            state = self.get_optimistic_state()
        else:
            state = self.get_finalized_state()
        nonce = big_endian_to_int(state.get_storage(self.addr, NONCE))
        if nonce <= self._last_nonce:
            # potentially sending back-to-back transactions so increment the
            # nonce.
            nonce = self._last_nonce + 1
        self._last_nonce = nonce
        return nonce

    def join(self):
        vcode = mk_validation_code(self.key)
        # Make the transaction to join as a Casper guardian
        txdata = casper_ct.encode('join', [vcode])
        tx = mk_transaction(
            self.get_nonce(),
            gasprice=25 * 10**9,
            gas=1000000,
            to=CASPER,
            value=1500 * 10**18,
            data=txdata,
            key=self.key,
            create=True,
        )
        DEBUG('Joining Guarding Pool',
              tx_hash=tx.hash.encode('hex'),
              address=self.addr.encode('hex'))
        self.add_transaction(tx)

        # Icky grossness, do this better.
        self._joined_at_block = len(self.blocks)

    # Compute as many future block proposers as possible
    def add_proposers(self):
        h = len(self.finalized_hashes) - 1
        while h >= 0 and self.stateroots[h] in (None, '\x00' * 32):
            h -= 1
        state = State(self.stateroots[h] if h >= 0 else self.genesis_state_root, self.db)
        maxh = self.max_finalized_height + ENTER_EXIT_DELAY - 1
        for h in range(len(self.proposers), maxh):
            self.proposers.append(get_guardian_index(state, h))
            if self.proposers[-1] == self.index:
                self.next_block_to_produce = h
                return
        self.next_block_to_produce = None

    def receive_block(self, block):
        # If you already processed the block, return
        if block.hash in self.objects:
            return
        DEBUG('Received block',
              number=block.number,
              hash=block.hash.encode('hex')[:16],
              recipient=self.index)
        # Update the lengths of our main lists to make sure they can store
        # the data we will be calculating
        while len(self.blocks) <= block.number:
            self.blocks.append(None)
            self.stateroots.append(None)
            self.finalized_hashes.append(None)
            self.probs.append(0.5)
        # If we are not sufficiently synced, try to sync previous blocks first
        if block.number >= self.calc_state_roots_from + ENTER_EXIT_DELAY - 1:
            sys.stderr.write('Not sufficiently synced to receive this block (%d)\n' % block.number)
            if self.last_time_sent_getblocks < self.now - 5:
                DEBUG('asking for blocks', index=self.index)
                self.network.broadcast(self, rlp.encode(NetworkMessage(NM_GETBLOCKS, [encode_int(self.max_finalized_height + 1)])))
                self.last_time_sent_getblocks = self.now
            return
        # If the block is invalid, return
        check_state = self.get_state_at_height(block.number - ENTER_EXIT_DELAY + 1)
        if not is_block_valid(check_state, block):
            sys.stderr.write("ERR: Received invalid block: %d %s\n" % (block.number, block.hash.encode('hex')[:16]))
            return
        check_state2 = self.get_state_at_height(min(self.max_finalized_height, self.calc_state_roots_from - 1))
        # Try to update the set of guardians
        vs = call_casper(check_state2, 'getGuardianSignups', [])
        if vs > self.guardian_signups:
            DEBUG('updating guardian signups', shouldbe=vs, lastcached=self.guardian_signups)
            self.guardian_signups = vs
            self.update_guardian_set(check_state2)
        # Add the block to our list of blocks
        if not self.blocks[block.number]:
            self.blocks[block.number] = block
        else:
            DEBUG('Caught a double block!')
            bytes1 = rlp.encode(self.blocks[block.number].header)
            bytes2 = rlp.encode(block.header)
            new_tx = Transaction(CASPER, 500000 + 1000 * len(bytes1) + 1000 * len(bytes2),
                                 data=casper_ct.encode('slashBlocks', [bytes1, bytes2]))
            self.add_transaction(new_tx, track=True)
        # Store the block as having been received
        self.objects[block.hash] = block
        self.time_received[block.hash] = self.now
        self.recently_discovered_blocks.append(block.number)
        time_delay = self.now - (self.genesis_time + BLKTIME * block.number)
        DEBUG("Received good block",
              height=block.number,
              hash=block.hash.encode('hex')[:16],
              time_delay=time_delay)
        # Add transactions to the unconfirmed transaction index
        for i, g in enumerate(block.transaction_groups):
            for j, tx in enumerate(g):
                if tx.hash not in self.finalized_txindex:
                    if tx.hash not in self.unconfirmed_txindex:
                        self.unconfirmed_txindex[tx.hash] = (tx, [])
                    self.unconfirmed_txindex[tx.hash][1].append((block.number, block.hash, i, j))
        # Re-broadcast the block
        self.network.broadcast(self, rlp.encode(NetworkMessage(NM_BLOCK, [rlp.encode(block)])))
        # Bet
        if (self.index % VALIDATOR_ROUNDS) == (block.number % VALIDATOR_ROUNDS):
            DEBUG("betting", index=self.index, height=block.number)
            self.mkbet()

    # Try to update the set of guardians
    def update_guardian_set(self, check_state):
        for i in range(call_casper(check_state, 'getNextGuardianIndex', [])):
            ctr = call_casper(check_state, 'getGuardianCounter', [i])
            # Ooh, we found a new guardian
            if ctr not in self.counters:
                self.counters[ctr] = 1
                ih = call_casper(check_state, 'getGuardianInductionHeight', [i])
                valaddr = call_casper(check_state, 'getGuardianAddress', [i])
                valcode = call_casper(check_state, 'getGuardianValidationCode', [i])
                self.opinions[i] = Opinion(valcode, i, '\x00' * 32, 0, ih)
                self.opinions[i].deposit_size = call_casper(check_state, 'getGuardianDeposit', [i])
                DEBUG('Guardian inducted', index=i, address=valaddr, my_index=self.index)
                self.bets[i] = {}
                self.highest_bet_processed[i] = -1
                # Is the new guardian me?
                if valaddr == self.addr.encode('hex'):
                    self.index = i
                    self.add_proposers()
                    self.induction_height = ih
                    DEBUG('I have been inducted!', index=self.index)
        DEBUG('Tracking %d opinions' % len(self.opinions))

    def receive_bet(self, bet):
        # Do not process the bet if (i) we already processed it, or (ii) it
        # comes from a guardian not in the current guardian set
        if bet.hash in self.objects or bet.index not in self.opinions:
            return
        # Record when the bet came and that it came
        self.objects[bet.hash] = bet
        self.time_received[bet.hash] = self.now
        # Re-broadcast it
        self.network.broadcast(self, rlp.encode(NetworkMessage(NM_BET, [bet.serialize()])))
        # Do we have a duplicate? If so, slash it
        if bet.seq in self.bets[bet.index]:
            DEBUG('Caught a double bet!')
            bytes1 = self.bets[bet.index][bet.seq].serialize()
            bytes2 = bet.serialize()
            new_tx = Transaction(CASPER, 500000 + 1000 * len(bytes1) + 1000 * len(bytes2),
                                 data=casper_ct.encode('slashBets', [bytes1, bytes2]))
            self.add_transaction(new_tx, track=True)
        # Record it
        self.bets[bet.index][bet.seq] = bet
        # If we have an unbroken chain of bets from 0 to N, and last round
        # we had an unbroken chain only from 0 to M, then process bets
        # M+1...N. For example, if we had bets 0, 1, 2, 4, 5, 7, now we
        # receive 3, then we assume bets 0, 1, 2 were already processed
        # but now process 3, 4, 5 (but NOT 7)
        DEBUG('receiving a bet', seq=bet.seq, index=bet.index, recipient=self.index)
        proc = 0
        while (self.highest_bet_processed[bet.index] + 1) in self.bets[bet.index]:
            assert self.opinions[bet.index].process_bet(self.bets[bet.index][self.highest_bet_processed[bet.index] + 1])
            self.highest_bet_processed[bet.index] += 1
            proc += 1
        # Sanity check
        for i in range(0, self.highest_bet_processed[bet.index] + 1):
            assert i in self.bets[bet.index]
        assert self.opinions[bet.index].seq == self.highest_bet_processed[bet.index] + 1
        # If we did not process any bets after receiving a bet, that
        # implies that we are missing some bets. Ask for them.
        if not proc and self.last_asked_for_bets.get(bet.index, 0) < self.now + 10:
            self.network.send_to_one(self, rlp.encode(NetworkMessage(NM_BET_REQUEST, map(encode_int, [bet.index, self.highest_bet_processed[bet.index] + 1]))))
            self.last_asked_for_bets[bet.index] = self.now

    # Make a bet that signifies that we do not want to make any more bets
    def withdraw(self):
        o = sign_bet(Bet(self.index, 2**256 - 1, [], [], [], [], self.prevhash, self.seq, ''), self.key)
        payload = rlp.encode(NetworkMessage(NM_BET, [o.serialize()]))
        self.prevhash = o.hash
        self.seq += 1
        self.network.broadcast(self, payload)
        self.receive_bet(o)
        self.former_index = self.index
        self.index = -1
        self.withdrawn = True

    # Take one's ether out
    def finalizeWithdrawal(self):
        txdata = casper_ct.encode('withdraw', [self.former_index])
        tx = mk_transaction(self.get_nonce(), 1, 1000000, CASPER, 0, txdata, self.key, True)
        v = tx_state_transition(self.genesis, tx)  # NOQA

    # Compute as many state roots as possible
    def recalc_state_roots(self):
        recalc_limit = MAX_RECALC if self.calc_state_roots_from > len(self.blocks) - 20 else MAX_LONG_RECALC
        frm = self.calc_state_roots_from
        DEBUG('recalculating', limit=recalc_limit, want=len(self.blocks) - frm)
        run_state = self.get_state_at_height(frm - 1)
        for h in range(frm, len(self.blocks))[:recalc_limit]:
            prevblknum = big_endian_to_int(run_state.get_storage(BLKNUMBER, '\x00' * 32))
            assert prevblknum == h
            prob = self.probs[h] or 0.5
            block_state_transition(run_state, self.blocks[h] if prob >= 0.5 else None)
            self.stateroots[h] = run_state.root
            blknum = big_endian_to_int(run_state.get_storage(BLKNUMBER, '\x00' * 32))
            assert blknum == h + 1
        # If there are some state roots that we have not calculated, just leave them empty
        for h in range(frm + recalc_limit, len(self.blocks)):
            self.stateroots[h] = '\x00' * 32
        # Where to calculate state roots from next time
        self.calc_state_roots_from = min(frm + recalc_limit, len(self.blocks))
        # Check integrity
        for i in range(self.calc_state_roots_from):
            assert self.stateroots[i] not in ('\x00' * 32, None)

    # Get a state object that we run functions or process blocks against

    # finalized version (safer)
    def get_finalized_state(self):
        h = min(self.calc_state_roots_from - 1, self.max_finalized_height)
        return State(self.stateroots[h] if h >= 0 else self.genesis_state_root, self.db)

    # optimistic version (more up-to-date)
    def get_optimistic_state(self):
        h = self.calc_state_roots_from - 1
        return State(self.stateroots[h] if h >= 0 else self.genesis_state_root, self.db)

    # Get a state object at a given height
    def get_state_at_height(self, h):
        return State(self.stateroots[h] if h >= 0 else self.genesis_state_root, self.db)

    # Construct a bet
    def mkbet(self):
        # Bet at most once every two seconds to save on computational costs
        if self.now < self.last_bet_made + 2:
            return
        self.last_bet_made = self.now
        # Height at which to start signing
        sign_from = max(0, self.max_finalized_height)
        # Keep track of the lowest state root that we should change
        DEBUG('Making probs', frm=sign_from, to=len(self.blocks) - 1)
        # State root probs
        srp = []
        srp_accum = FINALITY_HIGH
        # Bet on each height independently using our betting strategy
        for h in range(sign_from, len(self.blocks)):
            # Get the probability that we should bet
            prob, new_block_hash, ask = \
                bet_at_height(self.opinions,
                              h,
                              [self.blocks[h]] if self.blocks[h] else [],
                              self.time_received,
                              self.genesis_time,
                              self.now)
            # Do we need to ask for a block from the network?
            if ask and (new_block_hash not in self.last_asked_for_block or self.last_asked_for_block[new_block_hash] < self.now + 12):
                DEBUG('Suspiciously missing a block, asking for it explicitly.',
                      number=h, hash=new_block_hash.encode('hex')[:16])
                self.network.broadcast(self, rlp.encode(NetworkMessage(NM_GETBLOCK, [new_block_hash])))
                self.last_asked_for_block[h] = self.now
            # Did our preferred block hash change?
            if self.blocks[h] and new_block_hash != self.blocks[h].hash:
                if new_block_hash not in (None, '\x00' * 32) and new_block_hash in self.objects:
                    DEBUG('Changing block selection', height=h,
                          pre=self.blocks[h].hash[:8].encode('hex'),
                          post=new_block_hash[:8].encode('hex'))
                    assert self.objects[new_block_hash].number == h
                    self.blocks[h] = self.objects[new_block_hash]
                    self.recently_discovered_blocks.append(h)
            # If the probability of a block flips to the other side of 0.5,
            # that means that we should recalculate the state root at least
            # from that point (and possibly earlier)
            if (
                    (prob - 0.5) * (self.probs[h] - 0.5) <= 0 or
                    (
                        self.probs[h] >= 0.5 and
                        h in self.recently_discovered_blocks
                    )) and h < self.calc_state_roots_from:
                DEBUG('Rewinding', num_blocks=self.calc_state_roots_from - h)
                self.calc_state_roots_from = h
            self.probs[h] = prob
            # Compute the state root probabilities
            if srp_accum == FINALITY_HIGH and prob >= FINALITY_HIGH:
                srp.append(FINALITY_HIGH)
            else:
                srp_accum *= prob
                srp.append(max(srp_accum, FINALITY_LOW))
            # Finalized!
            if prob < FINALITY_LOW or prob > FINALITY_HIGH:
                DEBUG('Finalizing', height=h, my_index=self.index)
                # Set the finalized hash
                self.finalized_hashes[h] = self.blocks[h].hash if prob > FINALITY_HIGH else '\x00' * 32
                # Try to increase the max finalized height
                while h == self.max_finalized_height + 1:
                    self.max_finalized_height = h
                    DEBUG('Increasing max finalized height', new_height=h)
                    if not h % 10:
                        for i in self.opinions.keys():
                            self.opinions[i].deposit_size = call_casper(self.get_optimistic_state(), 'getGuardianDeposit', [i])
        # Recalculate state roots
        rootstart = max(self.calc_state_roots_from, self.induction_height)
        self.recalc_state_roots()
        # Sanity check
        assert len(self.probs) == len(self.blocks) == len(self.stateroots)
        # If we are supposed to actually make a bet... (if not, all the code
        # above is simply for personal information, ie. for a listening node
        # to determine its opinion on what the correct chain is)
        if self.index >= 0 and len(self.blocks) > self.induction_height and not self.withdrawn and len(self.recently_discovered_blocks):
            # Create and sign the bet
            blockstart = max(min(self.recently_discovered_blocks), self.induction_height)
            probstart = min(max(sign_from, self.induction_height), blockstart, rootstart)
            srprobstart = max(sign_from, self.induction_height) - sign_from
            assert len(srp[srprobstart:]) <= len(self.probs[probstart:])
            assert srprobstart + sign_from >= probstart
            o = sign_bet(Bet(
                self.index,
                len(self.blocks) - 1,
                self.probs[probstart:][::-1],
                [x.hash if x else '\x00' * 32 for x in self.blocks[blockstart:]][::-1],
                self.stateroots[rootstart:][::-1],
                [x if (self.stateroots[i] != '\x00' * 32) else FINALITY_LOW for i, x in enumerate(srp)][srprobstart:][::-1],
                self.prevhash,
                self.seq,
                '',
            ), self.key)
            # Reset the recently discovered blocks array, so that we do not needlessly resubmit hashes
            self.recently_discovered_blocks = []
            # Update my prevhash and seq
            self.prevhash = o.hash
            self.seq += 1
            # Send the bet over the network
            payload = rlp.encode(NetworkMessage(NM_BET, [o.serialize()]))
            self.network.broadcast(self, payload)
            # Process it myself
            self.receive_bet(o)
            # Create two bets of the same seq (for testing purposes)
            if self.seq > self.double_bet_suicide and len(o.probs):
                DEBUG('MOO HA HA DOUBLE BETTING')
                o.probs[0] *= 0.9
                o = sign_bet(o, self.key)
                payload = rlp.encode(NetworkMessage(NM_BET, [o.serialize()]))
                self.network.broadcast(self, payload)

    # Upon receiving any kind of network message
    # Arguments: payload, ID of the node sending the message (used to
    # direct-send replies back)
    def on_receive(self, objdata, sender_id):
        obj = rlp_decode(objdata, NetworkMessage)
        if obj.typ == NM_BLOCK:
            blk = rlp_decode(obj.args[0], Block)
            self.receive_block(blk)
        elif obj.typ == NM_BET:
            bet = Bet.deserialize(obj.args[0])
            self.receive_bet(bet)
        elif obj.typ == NM_BET_REQUEST:
            index = big_endian_to_int(obj.args[0])
            seq = big_endian_to_int(obj.args[1])
            if index not in self.bets:
                return
            bets = [self.bets[index][x] for x in range(seq, self.highest_bet_processed[index] + 1)]
            if len(bets):
                messages = [rlp.encode(NetworkMessage(NM_BET, [bet.serialize()])) for bet in bets]
                self.network.direct_send(self, sender_id, rlp.encode(NetworkMessage(NM_LIST, messages)))
        elif obj.typ == NM_TRANSACTION:
            tx = rlp_decode(obj.args[0], Transaction)
            if self.should_i_include_transaction(tx):
                self.add_transaction(tx)
        elif obj.typ == NM_GETBLOCK:
            # Asking for block by number:
            if len(obj.args[0]) < 32:
                blknum = big_endian_to_int(obj.args[0])
                if blknum < len(self.blocks) and self.blocks[blknum]:
                    self.network.direct_send(self, sender_id, rlp.encode(NetworkMessage(NM_BLOCK, [rlp.encode(self.blocks[blknum])])))
            # Asking for block by hash
            else:
                o = self.objects.get(obj.args[0], None)
                if isinstance(o, Block):
                    self.network.direct_send(self, sender_id, rlp.encode(NetworkMessage(NM_BLOCK, [rlp.encode(o)])))
        elif obj.typ == NM_GETBLOCKS:
            blknum = big_endian_to_int(obj.args[0])
            messages = []
            for h in range(blknum, len(self.blocks))[:30]:
                if self.blocks[h]:
                    messages.append(rlp.encode(NetworkMessage(NM_BLOCK, [rlp.encode(self.blocks[h])])))
            self.network.direct_send(self, sender_id, rlp.encode(NetworkMessage(NM_LIST, messages)))
            if blknum < len(self.blocks) and self.blocks[blknum]:
                self.network.direct_send(self, sender_id, rlp.encode(NetworkMessage(NM_BLOCK, [rlp.encode(self.blocks[blknum])])))
        elif obj.typ == NM_LIST:
            for x in obj.args:
                self.on_receive(x, sender_id)
        elif obj.typ == NM_FAUCET:
            to_addr = rlp_decode(obj.args[0], address)
            amount = big_endian_to_int(obj.args[1])
            state = self.get_optimistic_state()
            balance = big_endian_to_int(state.get_storage(ETHER, self.addr))
            if balance >= amount * 2:
                DEBUG("Fauceting ether", to_addr=to_addr.encode('hex'), amount=amount)
                self.send_ether(to_addr, amount)
            else:
                DEBUG("Relaying Faucet request", to_addr=to_addr.encode('hex'), amount=amount)
                self.network.send_to_one(self, rlp.encode(NetworkMessage(NM_FAUCET, [rlp.encode(to_addr, address), encode_int(amount)])))

    def send_ether(self, to_addr, amount):
        tx = mk_transaction(
            self.get_nonce(),
            gasprice=25 * 10**9,
            gas=1000000,
            to=to_addr,
            value=amount,
            data='',
            key=self.key,
            create=False,
        )
        self.add_transaction(tx)

    def request_ether(self, amount):
        nm = NetworkMessage(NM_FAUCET, [rlp.encode(self.addr, address), encode_int(amount)])
        self.network.send_to_one(self, rlp.encode(nm))
        self.last_faucet_request = len(self.blocks)

    def should_i_include_transaction(self, tx):
        check_state = self.get_optimistic_state()
        o = tx_state_transition(check_state, tx, override_gas=250000 + tx.intrinsic_gas, breaking=True)
        if not o:
            DEBUG('No output from running transaction',
                  hash=tx.hash.encode('hex')[:16])
            return False
        output = ''.join(map(chr, o))
        # Make sure that the account code matches
        if get_code(check_state, tx.addr).rstrip('\x00') != mandatory_account_evm:
            DEBUG('Account EVM mismatch',
                  hash=tx.hash.encode('hex')[:16],
                  shouldbe=mandatory_account_evm,
                  reallyis=get_code(check_state, tx.addr))
            return False
        # Make sure that the right gas price is in memory (and implicitly that the tx succeeded)
        if len(output) < 32:
            DEBUG('Min gas price not found in output, not including transaction',
                  hash=tx.hash.encode('hex')[:16])
            return False
        # Make sure that the gas price is sufficient
        if big_endian_to_int(output[:32]) < self.min_gas_price:
            DEBUG('Gas price too low',
                  shouldbe=self.min_gas_price,
                  reallyis=big_endian_to_int(output[:32]),
                  hash=tx.hash.encode('hex')[:16])
            return False
        DEBUG('Transaction passes, should be included',
              hash=tx.hash.encode('hex')[:16])
        return True

    def add_transaction(self, tx, track=False):
        if tx.hash not in self.objects or self.time_received.get(tx.hash, 0) < self.now - 15:
            DEBUG('Received transaction', hash=tx.hash.encode('hex')[:16])
            self.objects[tx.hash] = tx
            self.time_received[tx.hash] = self.now
            self.txpool[tx.hash] = tx
            if track:
                self.tracked_tx_hashes.append(tx.hash)
            self.network.broadcast(self, rlp.encode(NetworkMessage(NM_TRANSACTION, [rlp.encode(tx)])))

    def make_block(self):
        # Transaction inclusion algorithm
        gas = GASLIMIT
        txs = []
        # Try to include transactions in txpool
        for h, tx in self.txpool.items():
            # If a transaction is not in the unconfirmed index AND not in the
            # finalized index, then add it
            if h not in self.unconfirmed_txindex and h not in self.finalized_txindex:
                DEBUG('Adding transaction',
                      hash=tx.hash.encode('hex')[:16],
                      blknum=self.next_block_to_produce)
                if tx.gas > gas:
                    break
                txs.append(tx)
                gas -= tx.gas
        # Publish most recent bets to the blockchain
        h = 0
        while h < len(self.stateroots) and self.stateroots[h] not in (None, '\x00' * 32):
            h += 1
        latest_state_root = self.stateroots[h - 1] if h else self.genesis_state_root
        assert latest_state_root not in ('\x00' * 32, None)
        latest_state = State(latest_state_root, self.db)
        ops = self.opinions.items()
        random.shuffle(ops)
        DEBUG('Producing block',
              number=self.next_block_to_produce,
              known=len(self.blocks),
              check_root_height=h - 1)
        for i, o in ops:
            latest_bet = call_casper(latest_state, 'getGuardianSeq', [i])
            bet_height = latest_bet
            while bet_height in self.bets[i]:
                DEBUG('Inserting bet', seq=latest_bet, index=i)
                bet = self.bets[i][bet_height]
                new_tx = Transaction(CASPER, 200000 + 6600 * len(bet.probs) + 10000 * len(bet.blockhashes + bet.stateroots),
                                     data=bet.serialize())
                if bet.max_height == 2**256 - 1:
                    self.tracked_tx_hashes.append(new_tx.hash)
                if new_tx.gas > gas:
                    break
                txs.append(new_tx)
                gas -= new_tx.gas
                bet_height += 1
            if o.seq < latest_bet:
                self.network.send_to_one(self, rlp.encode(NetworkMessage(NM_BET_REQUEST, map(encode_int, [i, o.seq + 1]))))
                self.last_asked_for_bets[i] = self.now
        # Process the unconfirmed index for the transaction. Note that a
        # transaction could theoretically get included in the chain
        # multiple times even within the same block, though if the account
        # used to process the transaction is sane the transaction should
        # fail all but one time
        for h, (tx, positions) in self.unconfirmed_txindex.items():
            i = 0
            while i < len(positions):
                # We see this transaction at index `index` of block number `blknum`
                blknum, blkhash, groupindex, txindex = positions[i]
                if self.stateroots[blknum] in (None, '\x00' * 32):
                    i += 1
                    continue
                # Probability of the block being included
                p = self.probs[blknum]
                # Try running it
                if p > 0.95:
                    grp_shard = self.blocks[blknum].summaries[groupindex].left_bound
                    logdata = State(self.stateroots[blknum], self.db).get_storage(shardify(LOG, grp_shard), txindex)
                    logresult = big_endian_to_int(rlp.decode(rlp.descend(logdata, 0)))
                    # If the transaction passed and the block is finalized...
                    if p > 0.9999 and logresult == 2:
                        DEBUG('Transaction finalized',
                              hash=tx.hash.encode('hex')[:16],
                              blknum=blknum,
                              blkhash=blkhash.encode('hex')[:16],
                              grpindex=groupindex,
                              txindex=txindex)
                        # Remove it from the txpool
                        if h in self.txpool:
                            del self.txpool[h]
                        # Add it to the finalized index
                        if h not in self.finalized_txindex:
                            self.finalized_txindex[h] = (tx, [])
                        self.finalized_txindex[h][1].append((blknum, blkhash, groupindex, txindex, rlp.decode(logdata)))
                        positions.pop(i)
                    # If the transaction was included but exited with an error (eg. due to a sequence number mismatch)
                    elif p > 0.95 and logresult == 1:
                        positions.pop(i)
                        self.tx_exceptions[h] = self.tx_exceptions.get(h, 0) + 1
                        DEBUG('Transaction inclusion finalized but transaction failed for the %dth time' % self.tx_exceptions[h],
                              hash=tx.hash.encode('hex')[:16])
                        # 10 strikes and we're out
                        if self.tx_exceptions[h] >= 10:
                            if h in self.txpool:
                                del self.txpool[h]
                    # If the transaction failed (eg. due to OOG from block gaslimit),
                    # remove it from the unconfirmed index, but not the expool, so
                    # that we can try to add it again
                    elif logresult == 0:
                        DEBUG('Transaction finalization attempt failed', hash=tx.hash.encode('hex')[:16])
                        positions.pop(i)
                    else:
                        i += 1
                # If the block that the transaction was in didn't pass through,
                # remove it from the unconfirmed index, but not the expool, so
                # that we can try to add it again
                elif p < 0.05:
                    DEBUG('Transaction finalization attempt failed', hash=tx.hash.encode('hex')[:16])
                    positions.pop(i)
                # Otherwise keep the transaction in the unconfirmed index
                else:
                    i += 1
            if len(positions) == 0:
                del self.unconfirmed_txindex[h]
        # Produce the block
        b = sign_block(Block(transactions=txs, number=self.next_block_to_produce, proposer=self.addr), self.key)
        # Broadcast it
        self.network.broadcast(self, rlp.encode(NetworkMessage(NM_BLOCK, [rlp.encode(b)])))
        self.receive_block(b)
        # If byzantine, produce two blocks
        if b.number >= self.double_block_suicide:
            DEBUG('## Being evil and making two blocks!!\n\n')
            new_tx = mk_transaction(self.get_nonce(), 1, 1000000, '\x33' * ADDR_BYTES, 1, '', self.key, True)
            txs2 = [tx for tx in txs] + [new_tx]
            b2 = sign_block(Block(transactions=txs2, number=self.next_block_to_produce, proposer=self.addr), self.key)
            self.network.broadcast(self, rlp.encode(NetworkMessage(NM_BLOCK, [rlp.encode(b2)])))
        # Extend the list of block proposers
        self.last_block_produced = self.next_block_to_produce
        self.add_proposers()
        # Log it
        time_delay = self.now - (self.genesis_time + BLKTIME * b.number)
        DEBUG('Making block', my_index=self.index, number=b.number,
              hash=b.hash.encode('hex')[:16], time_delay=time_delay)
        return b

    # Run every tick
    def tick(self):
        # DEBUG('bet tick called', at=self.now, id=self.id, index=self.index)
        mytime = self.now

        # We may not be a validator so potentially exit early
        if self.index < 0:
            state = self.get_finalized_state()
            balance = big_endian_to_int(state.get_storage(ETHER, self.addr))

            if balance < 1500 * 10**18:
                DEBUG("Account Balance:", addr=self.addr.encode('hex'), balance=balance)
                if not self.network.peers:
                    DEBUG("No peers yet.  Delaying Faucet Request")
                elif self.last_faucet_request < 0 or self.max_finalized_height > self.last_faucet_request + 5:
                    if not self.blocks:
                        DEBUG('Waiting for some blocks before requesting Ether')
                    else:
                        DEBUG('Requesting Ether')
                        self.request_ether(1600 * 10**18)
                else:
                    DEBUG('Waiting on faucet ether request')
            elif not self.blocks:
                DEBUG('delaying joining pool until a few blocks have shown up.')
            elif len(self.blocks) < self.join_at_block:
                DEBUG('Waiting to join pool', join_at_block=self.join_at_block)
            elif self._joined_at_block < 0:
                self.join()
            else:
                DEBUG('updating guardian set',
                      joined_at_block=self._joined_at_block,
                      index=self.index,
                      induction_height=self.induction_height)
                self.update_guardian_set(self.get_optimistic_state())

        # If (i) we should be making blocks, and (ii) the time has come to
        # produce a block, then produce a block
        if self.index >= 0 and self.next_block_to_produce is not None:
            target_time = self.genesis_time + BLKTIME * self.next_block_to_produce
            # DEBUG('maybe I should make a block', at=self.now, target_time=target_time )
            if mytime >= target_time:
                DEBUG('making a block')
                self.recalc_state_roots()
                self.make_block()
        elif self.next_block_to_produce is None:
            # DEBUG('add_prop', at=self.now, id=self.id)
            self.add_proposers()
        if self.last_bet_made < self.now - BLKTIME * VALIDATOR_ROUNDS * 1.5:
            # DEBUG('mk bet', at=self.now, id=self.id)
            self.mkbet()

        self.db.commit()

    @property
    def now(self):
        return self.network.now