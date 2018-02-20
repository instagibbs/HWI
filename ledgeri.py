# Ledger interaction script

from hwi import HardwareWalletClient
from btchip.btchip import *
from btchip.btchipUtils import *
import base64
import json
import struct
import base58
from serializations import hash256, hash160, ser_uint256, PSBT, CTransaction
import binascii

# This class extends the HardwareWalletClient for Ledger Nano S specific things
class LedgerClient(HardwareWalletClient):

    # device is an HID device that has already been opened.
    # hacked in device support using btchip-python
    def __init__(self, device):
        super(LedgerClient, self).__init__(device)
        dongle = getDongle(True)
        self.app = btchip(dongle)
        self.device = device

    # Must return a dict with the xpub
    # Retrieves the public key at the specified BIP 32 derivation path
    def get_pubkey_at_path(self, path):
        path = path[2:]
        # This call returns raw uncompressed pubkey, chaincode
        pubkey = self.app.getWalletPublicKey(path)
        if path != "":
            parent_path = ""
            for ind in path.split("/")[:-1]:
                parent_path += ind+"/"
            parent_path = parent_path[:-1]

            # Get parent key fingerprint
            parent = self.app.getWalletPublicKey(parent_path)
            fpr = hash160(compress_public_key(parent["publicKey"]))[:4]

            # Compute child info
            childstr = path.split("/")[-1]
            hard = 0
            if childstr[-1] == "'":
                childstr = childstr[:-1]
                hard = 0x80000000
            child = struct.pack(">I", int(childstr)+hard)
        # Special case for m
        else:
            child = "00000000".decode('hex')
            fpr = child

        chainCode = pubkey["chainCode"]
        publicKey = compress_public_key(pubkey["publicKey"])

        depth = len(path.split("/")) if len(path) > 0 else 0
        depth = struct.pack("B", depth)

        version = "0488B21E".decode('hex')
        extkey = version+depth+fpr+child+chainCode+publicKey
        checksum = hash256(extkey)[:4]

        return json.dumps({"xpub":base58.encode(extkey+checksum)})

    # Must return a hex string with the signed transaction
    # The tx must be in the combined unsigned transaction format
    # Current only supports segwit signing
    def sign_tx(self, tx):
        c_tx = CTransaction(tx.tx)
        tx_bytes = c_tx.serialize_with_witness()
        
        # Master key fingerprint
        # FIXME deal with only knowing xpub version master?
        master_fpr = hash160(compress_public_key(self.app.getWalletPublicKey('44\'/0\'/0\'')["publicKey"]))[:4]

        # An entry per input, each with 0 to many keys to sign with
        all_signature_attempts = []

        # Inputs during segwit preprocessing step
        segwit_inputs = []

        # Length check barfs due to printing(?) just iterate
        script_codes = [[]]*len(c_tx.vin)

        # Detect changepath, (p2sh-)p2(w)pkh only
        change_path = '0'
        for txout, i_num in zip(c_tx.vout, range(len(c_tx.vout))):

            # Find which wallet key could be change based on hdsplit: m/.../1/k
            # Wallets shouldn't be sending to change address as user action
            # otherwise this will get confused
            for pubkey, path in tx.hd_keypaths.items():
                if path[0] == master_fpr and len(path) > 2 and path[-2] == 1:
                    # For possible matches, check if pubkey matches possible template
                    if hash160(pubkey) in txout.scriptPubKey or hash160("160014".decode('hex')+hash160(pubkey)) in txout.scriptPubKey:
                        for index in path[1:]:
                            change_path += str(index)+"/"
                        change_path = change_path[:-1]


        for txin, psbt_in, i_num in zip(c_tx.vin, tx.inputs, range(len(c_tx.vin))):

            seq = format(txin.nSequence, 'x')
            seq = seq.zfill(8)
            seq = bytearray(seq.decode('hex'))
            seq.reverse()
            seq_hex = ''.join('{:02x}'.format(x) for x in seq)

            # We will not attempt to sign non-witness inputs but
            # need information for pre-processing
            if psbt_in.non_witness_utxo:
                segwit_inputs.append({"value":txin.prevout.serialize()+struct.pack("<Q", psbt_in.non_witness_utxo.vout[txin.prevout.n].nValue), "witness":True, "sequence":seq_hex})
                continue
            else:
                segwit_inputs.append({"value":txin.prevout.serialize()+struct.pack("<Q", psbt_in.witness_utxo.nValue), "witness":True, "sequence":seq_hex})

            pubkeys = []
            signature_attempts = []

            scriptCode = b""
            witness_program = b""
            if psbt_in.witness_utxo.is_p2sh():
                # Look up redeemscript
                redeemscript = tx.redeem_scripts[psbt_in.witness_utxo.scriptPubKey[2:22]]
                witness_program += redeemscript
            else:
                witness_program += psbt_in.witness_utxo.scriptPubKey

            # Check if witness_program is script hash
            if len(witness_program) == 34 and ord(witness_program[0]) == 0x00 and ord(witness_program[1]) == 0x20:
                # look up witnessscript and set as scriptCode
                witnessscript = tx.witness_scripts[redeemscript[2:]]
                scriptCode += witnessscript
            else:
                scriptCode += b"\x76\xa9\x14"
                scriptCode += witness_program[2:]
                scriptCode += b"\x88\xac"

            # Save scriptcode for later signing
            script_codes[i_num] = scriptCode

            # Find which pubkeys could sign this input
            for pubkey in tx.hd_keypaths.keys():
                if hash160(pubkey) in scriptCode or pubkey in scriptCode:
                    pubkeys.append(pubkey)

            # Figure out which keys in inputs are from our wallet
            for pubkey in pubkeys:
                keypath = tx.hd_keypaths[pubkey]
                if master_fpr == struct.pack("<I", keypath[0]):
                    # Add the keypath strings
                    keypath_str = ''
                    for index in keypath[1:]:
                        keypath_str += str(index) + "/"
                    keypath_str = keypath_str[:-1]
                    signature_attempts.append([keypath_str, pubkey])
            
            all_signature_attempts.append(signature_attempts)

        # NOTE: This will likely get replaced on unified segwit/legacy signing firmware
        # Process them up front with all scriptcodes blank
        blank_script_code = bytearray()
        for i in range(len(segwit_inputs)):
            self.app.startUntrustedTransaction(i==0, i, segwit_inputs, blank_script_code, c_tx.nVersion)

        # Number of unused fields for Nano S, only changepath and transaction in bytes req
        outputData = self.app.finalizeInput("DUMMY", -1, -1, change_path, tx_bytes)

        # For each input we control do segwit signature
        for i in range(len(segwit_inputs)):
            for signature_attempt in all_signature_attempts[i]:
                self.app.startUntrustedTransaction(False, 0, [segwit_inputs[i]], script_codes[i], c_tx.nVersion)
                tx.inputs[i].partial_sigs[signature_attempt[1]] = self.app.untrustedHashSign(signature_attempt[0], "", c_tx.nLockTime, 0x01)

        # Send PSBT back
        return tx.serialize()

    # Must return a base64 encoded string with the signed message
    # The message can be any string
    def sign_message(self, message, keypath):
        keypath = keypath[2:]
        # First display on screen what address you're signing for
        self.app.getWalletPublicKey(keypath, True)
        self.app.signMessagePrepare(keypath, message)
        signature = self.app.signMessageSign()

        # Make signature into standard bitcoin format
        rLength = signature[3]
        r = signature[4 : 4 + rLength]
        sLength = signature[4 + rLength + 1]
        s = signature[4 + rLength + 2:]
        if rLength == 33:
            r = r[1:]
        if sLength == 33:
            s = s[1:]
        r = str(r)
        s = str(s)

        sig = chr(27 + 4 + (signature[0] & 0x01)) + r + s

        return json.dumps({"signature":base64.b64encode(sig)})

    # Setup a new device
    def setup_device(self):
        raise NotImplementedError('The Ledger Nano S does not support software setup')

    # Wipe this device
    def wipe_device(self):
        raise NotImplementedError('The Ledger Nano S does not support wiping via software')

# Avoid circular imports
from hwi import HardwareWalletClient
