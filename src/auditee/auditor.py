#!/usr/bin/env python
from __future__ import print_function

from base64 import b64decode, b64encode
from hashlib import md5, sha1, sha256
from os.path import join
from oracles import oracle_modulus
import binascii, hmac, os, platform,  tarfile
import random, re, sys, time, zipfile

#file system setup.
data_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.dirname(data_dir))
install_dir = os.path.dirname(os.path.dirname(data_dir))
sessions_dir = join(data_dir, 'sessions')
time_str = time.strftime('%d-%b-%Y-%H-%M-%S', time.gmtime())
current_session_dir = join(sessions_dir, time_str)
os.makedirs(current_session_dir)

global_tlsver = bytearray('\x03\x02')
global_use_gzip = True
global_use_slowaes = False
global_use_paillier = False
oracle_ba_modulus = None
oracle_int_modulus = None

def extract_audit_data(audit_filename):
    audit_data = {}
    with open(audit_filename,'rb') as f:
        header = f.read(29)
        if header != 'tlsnotary notarization file\n\n':
            raise Exception("Invalid file format")
        version = f.read(2)
        if version != '\x00\x01':
            raise Exception("Incompatible file version")
        audit_data['cipher_suite'] = shared.ba2int(f.read(2))
        audit_data['client_random'] = f.read(32)
        audit_data['server_random'] = f.read(32)
        audit_data['pms1'] = f.read(24)
        audit_data['pms2'] = f.read(24)
        audit_data['certs_len'] = shared.ba2int(f.read(3))
        audit_data['certs'] = f.read(audit_data['certs_len'])
        audit_data['tlsver'] = f.read(2)
        audit_data['initial_tlsver'] = f.read(2)
        response_len = shared.ba2int(f.read(8))
        audit_data['response'] = f.read(response_len)
        IV_len = shared.ba2int(f.read(2))
        if IV_len not in [258,16]:
            print ("IV length was: ", IV_len)
            raise Exception("Wrong IV format in audit file")
        audit_data['IV'] = f.read(IV_len)
        audit_data['oracle_modulus_len'] = f.read(2) #TODO can check this
        audit_data['signature'] = f.read(len(oracle_ba_modulus))
        audit_data['commit_hash'] = f.read(32)
        audit_data['oracle_modulus'] = f.read()
        if audit_data['oracle_modulus'] != oracle_ba_modulus:
            print ("file mod was: ", binascii.hexlify(audit_data['oracle_modulus']))
            print ("actual was: ", binascii.hexlify(oracle_ba_modulus))
            raise Exception("Unrecognized oracle")
    return audit_data

#unpack and check validity of Python modules
def first_run_check(modname,modhash):
    if not modhash: return
    mod_dir = join(data_dir, 'python', modname)
    if not os.path.exists(mod_dir):
        print ('Extracting '+modname + '.tar.gz...')
        with open(join(data_dir, 'python', modname+'.tar.gz'), 'rb') as f: tarfile_data = f.read()
        if md5(tarfile_data).hexdigest() !=  modhash:
            raise Exception ('Wrong hash')
        os.chdir(join(data_dir, 'python'))
        tar = tarfile.open(join(data_dir, 'python', modname+'.tar.gz'), 'r:gz')
        tar.extractall()
        tar.close()

if __name__ == "__main__":
    audit_filename = sys.argv[1]
    #for md5 hash, see https://pypi.python.org/pypi/<module name>/<module version>
    modules_to_load = {'rsa-3.1.4':'b6b1c80e1931d4eba8538fd5d4de1355',\
                       'pyasn1-0.1.7':'2cbd80fcd4c7b1c82180d3d76fee18c8',\
                       'slowaes':'','requests-2.3.0':'7449ffdc8ec9ac37bbcd286003c80f00'}
    for x,h in modules_to_load.iteritems():
        first_run_check(x,h)
        sys.path.append(join(data_dir, 'python', x))

    import rsa
    import pyasn1
    import requests
    from pyasn1.type import univ
    from pyasn1.codec.der import encoder, decoder
    from slowaes import AESModeOfOperation        
    import shared
    oracle_ba_modulus = bytearray('').join(map(chr,oracle_modulus))
    oracle_int_modulus = shared.ba2int(oracle_ba_modulus)     

    shared.load_program_config()
    
    if int(shared.config.get("General","gzip_disabled")) == 1:
        global_use_gzip = False   
    
    audit_data = extract_audit_data(audit_filename)
    #1. Verify notary pubkey - done in extract_audit_data
    print ('Notary pubkey OK')
    #2. Verify signature
    #First, extract the cert in DER form from the notarization file
    #Then, extract from the cert the modulus and server name (common name field)
    #To do this, we need to initialise the TLSNClientSession
    audit_session = shared.TLSNClientSession(ccs=audit_data['cipher_suite'],tlsver=audit_data['initial_tlsver'])
    first_cert_len = shared.ba2int(audit_data['certs'][:3])
    server_mod, server_exp = audit_session.extract_mod_and_exp(certDER=audit_data['certs'][3:3+first_cert_len], sn=True)
    print ('Processing data for server: ', audit_session.server_name)
    data_to_be_verified = audit_data['commit_hash'] + audit_data['pms2'] + shared.bi2ba(server_mod)
    data_to_be_verified = sha256(data_to_be_verified).digest()
    if not shared.verify_signature(data_to_be_verified, audit_data['signature'],oracle_int_modulus):
        print ('Audit FAILED. Signature is not verified.')
        exit()        
    print ('Notary signature OK')
    #3. Verify commitment hash.
    if not sha256(audit_data['response']).digest() == audit_data['commit_hash']:
        print ('Audit FAILED. Commitment hash does not match encrypted server response.')
        exit()
    print ('Commitment hash OK')
    #4 Decrypt html and check for mac errors.
    audit_session.unexpected_server_app_data_count = shared.ba2int(audit_data['response'][0])
    audit_session.tlsver = audit_data['tlsver']
    audit_session.client_random = audit_data['client_random']
    audit_session.server_random = audit_data['server_random']
    audit_session.pms1 = audit_data['pms1']
    audit_session.pms2 = audit_data['pms2']
    audit_session.p_auditee = shared.tls_10_prf('master secret'+audit_session.client_random+audit_session.server_random,
                                                first_half=audit_session.pms1)[0]
    audit_session.p_auditor = shared.tls_10_prf('master secret'+audit_session.client_random+audit_session.server_random,
                                                second_half=audit_session.pms2)[1]
    audit_session.set_master_secret_half()
    audit_session.do_key_expansion()
    audit_session.store_server_app_data_records(audit_data['response'][1:])   
    audit_session.IV_after_finished = (map(ord,audit_data['IV'][:256]),ord(audit_data['IV'][256]), \
            ord(audit_data['IV'][257])) if audit_data['cipher_suite'] in [4,5] else audit_data['IV'] 
    plaintext, bad_mac = audit_session.process_server_app_data_records(is_for_auditor=True)
    if bad_mac:
        print ('Audit FAILED. Decrypted data has bad HMACs.')
    print ('HTML decryption with correct HMACs OK.')
    plaintext = shared.dechunk_http(plaintext)
    plaintext = shared.gunzip_http(plaintext)
    #5 Display html + success.
    with open(join(current_session_dir,'audited.html'),'wb') as f:
        f.write(plaintext)
    #print out the info about the domain
    n_hexlified = binascii.hexlify(shared.bi2ba(server_mod))
    #pubkey in the format 09 56 23 ....
    n_write = " ".join(n_hexlified[i:i+2] for i in range(0, len(n_hexlified), 2))
    with open(join(current_session_dir,'domain_data.txt'), 'wb') as f: 
        f.write('Server name: '+audit_session.server_name + '\n\n'+'Server pubkey:' + '\n\n' + n_write+'\n')    

    print ("Audit passed! You can read the html at: ",
           join(current_session_dir,'audited.html'), 
           'and check the server certificate with the data provided in ',
           join(current_session_dir,'domain_data.txt'))
    
    