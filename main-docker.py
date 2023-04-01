#!/usr/bin/python
# -*- coding: utf-8 -*-

# NOTES:
#
# cacheposition.db - Indexes:
# 0 = Database Index of most recently checked message for Boost-A-Grams
# 1 = Index of Last Time (creationDate) since UNIX Epoch that EMail summary was sent
#

from collections import deque
from google.protobuf.json_format import MessageToJson
import datetime
import lightning_pb2 as ln
import lightning_pb2_grpc as lnrpc
import grpc
import codecs
import binascii
import os
import logging
import subprocess
import json
import base64
import pycurl
import smtplib

# Email Variables
EMAIL_ENABLE = False
SMTP_SERVER = "smtp.gmail.com" # Email Server
SMTP_PORT = 587 #Server Port (don't change!)
EMAIL_SEND_TO_ADDRESS = "<HERE>" # change this to the destination for the Emails
EMAIL_ACCOUNT_USERNAME = "<HERE>" # change this to match your gmail account
EMAIL_ACCOUNT_PASSWORD = "<PASSWORD HERE>"  # change this to match your gmail password
EMAIL_DAY = 6 # Monday = 0 --> Sunday = 6
EMAIL_DAILY = True
EMAIL_TIME = 1500 # BST = Midnight AEST, Crontab runs every 5 minutes but this can/will vary

# Definitions
TLVS = ["1629168", "7629169", "7629171", "1629175", "34349334", "133773310", "5428373484"]
TLVS_TO_EXTRACT = [["action", "boost"]]
EXTRAS_TO_EXTRACT = ["valueMsat", "creationDate"]
DATE_FIELDS = ["settleDate", "creationDate"]
MILLISAT_FIELDS = ["amtPaidMsat", "valueMsat"]
DEFAULT_NUM_MAX_INVOICES = 10000
DEFAULT_INDEX_OFFSET = 0
JSON_BOOSTAGRAMS = True
BOOSTAGRAM_FIELDS_TO_PUSH = ["app_name", "podcast", "episode", "message", "sender_name"]
REMEMBER_LAST_INDEX = True

# Pushover Notifications
PUSHOVER_ENABLE = True
PUSHOVER_USER_TOKEN = "<TOKEN HERE>"
PUSHOVER_API_TOKEN = "<TOKEN HERE>"

# Node Details
MACAROON_LOCATION = "/home/extractlv/settings/admin.macaroon" # Local Raspiblitz
TLSCERT_LOCATION = "/home/extractlv/settings/tls.cert" # Local Raspiblitz
NODE_ADDRESS = "johnchidgeysnode.duckdns.org" # Local Raspiblitz


def JSONtoString(JSONObject, ExtrasObject):
    message_string = 'Boost-A-Gram recieved, '
#datetime.datetime.utcfromtimestamp(int(invoice['creationDate'])))
    for key, value in ExtrasObject.items():
        if key in DATE_FIELDS:
            message_string += (key + ' = ' + str(datetime.datetime.utcfromtimestamp(int(value))) + ', ')
        elif key in MILLISAT_FIELDS:
            message_string += (key + ' in sats = ' + str(int(int(value)/1000)) + ', ')
        else:
            message_string += (key + ' = ' + str(value) + ', ')

    for key, value in JSONObject.items():
        if key in BOOSTAGRAM_FIELDS_TO_PUSH:
            message_string += (key + ' = ' + str(value) + ', ')
    message_string = message_string[:-2].replace("\n", " ")
    return message_string

# End JSONtoString


def JSONtoDictionary(JSONObject, ExtrasObject, LNDIndex):
    json_to_add = {}
    json_to_add['LNDIndex'] = LNDIndex
    for key, value in ExtrasObject.items():
        if key in DATE_FIELDS:
            json_to_add[key] = str(datetime.datetime.utcfromtimestamp(int(value)))
        elif key in MILLISAT_FIELDS:
            json_to_add[key] = int(int(value)/1000)
        else:
            json_to_add[key] = value
    for key, value in JSONObject.items():
        if key in BOOSTAGRAM_FIELDS_TO_PUSH:
            json_to_add[key] = str(value).replace('\n', ' ')
    return json_to_add

# End JSONtoDictionary


def main():
    os.environ["GRPC_SSL_CIPHER_SUITES"] = 'HIGH+ECDSA'

    with open("/home/extractlv/settings/settings.json", "r") as settingsfile:
        settingsdata = json.load(settingsfile)
        REMEMBER_LAST_INDEX = settingsdata['REMEMBER_LAST_INDEX']
        PUSHOVER_ENABLE = settingsdata['PUSHOVER_ENABLE']
        PUSHOVER_USER_TOKEN = settingsdata['PUSHOVER_USER_TOKEN']
        PUSHOVER_API_TOKEN = settingsdata['PUSHOVER_API_TOKEN']
        NODE_ADDRESS = settingsdata['NODE_ADDRESS']
#    print(settingsdata)
#    print(NODE_ADDRESS)

    # Lnd admin macaroon is at ~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon on Linux
    with open(os.path.expanduser(MACAROON_LOCATION), 'rb') as f:
        macaroon_bytes = f.read()
        macaroon = codecs.encode(macaroon_bytes, 'hex')

    # Lnd cert is at ~/.lnd/tls.cert on Linux
    cert = open(os.path.expanduser(TLSCERT_LOCATION), 'rb').read()
    creds = grpc.ssl_channel_credentials(cert)
    channel = grpc.secure_channel(NODE_ADDRESS + ':10009', creds)
    stub = lnrpc.LightningStub(channel)

    if REMEMBER_LAST_INDEX:
        # Open and read in previous index position
        invoicefile = os.path.expanduser('/home/extractlv/settings/cacheposition.db')
        if os.path.exists(invoicefile):
            with open(invoicefile) as invoicefiledata:
                invoicedataraw = invoicefiledata.readlines()
                indexarray = [i.strip() for i in invoicedataraw]
                indexoffset = int(indexarray[0])
        else:
            indexoffset = 0

#        print('Index: {value}'.format(value=indexarray))
#        print('Weekday: {value}'.format(value=datetime.datetime.today().weekday()))
#        print('Time: {value}'.format(value=datetime.datetime.today().strftime('%H%M')))
        request = ln.ListInvoiceRequest(
            pending_only=False,
            index_offset=indexoffset,
            num_max_invoices=DEFAULT_NUM_MAX_INVOICES,
            reversed=False,
        )
        invoice_database_index = indexoffset
    else:
        request = ln.ListInvoiceRequest(
            pending_only=False,
            index_offset=DEFAULT_INDEX_OFFSET,
            num_max_invoices=DEFAULT_NUM_MAX_INVOICES,
            reversed=False,
        )
        invoice_database_index = DEFAULT_INDEX_OFFSET

    # Retrieve and display the wallet balance
    invoice_list = stub.ListInvoices(request, metadata=[('macaroon', macaroon)])
    serialized_invoice_list = MessageToJson(invoice_list)
    dictionary_invoice_list = json.loads(serialized_invoice_list)
    boostagram_json_data = []
    if len(dictionary_invoice_list):
        for invoice in dictionary_invoice_list['invoices']:
            if "htlcs" in invoice and "isKeysend" in invoice:
                for htlc in invoice['htlcs']:
                    if "customRecords" in htlc:
                        for tlv_index, tlv_payload in htlc['customRecords'].items():
#                            print('TLV: {value}'.format(value=tlv_index))
                            if tlv_index in TLVS:
                                tlv_payload_UTF8 = base64.b64decode(tlv_payload).decode('utf8')
                                pushover_message = ''
                                extra_payload_data = {}
                                # Extract any Extra Record entries from the base Invoice first
                                try:
                                    for key in EXTRAS_TO_EXTRACT:
                                        extra_payload_data[key] = invoice[key]
                                except:
                                    pass

                                # Extract all TLV Record entries
                                try:
                                    tlv_payload_decoded = json.loads(tlv_payload_UTF8)
                                    for key, value in TLVS_TO_EXTRACT:
#                                        print('TLV0: {value}'.format(value=tlv_payload_decoded))
#                                        print('TLV0a: {value}'.format(value=value))
                                        if tlv_payload_decoded[key] in value:
                                            pushover_message = JSONtoString(tlv_payload_decoded, extra_payload_data)
                                            boostagram_json_data.append(JSONtoDictionary(tlv_payload_decoded, extra_payload_data, invoice_database_index))
#                                            print('TLVA: {value}'.format(value=tlv_payload_decoded))
#                                            print('TLV: {value}'.format(value=pushover_message))
                                        elif tlv_payload_decoded[key] and not value:
                                            pushover_message = JSONtoString(tlv_payload_decoded, extra_payload_data)
                                            boostagram_json_data.append(JSONtoDictionary(tlv_payload_decoded, extra_payload_data, invoice_database_index))
#                                            print('TLVB: {value}'.format(value=tlv_payload_decoded))
#                                            print('BJSON: {value}'.format(value=boostagram_json_data))
#                                            print('TLV: {value}'.format(value=pushover_message))
#                                            print('Sats: {value}'.format(value=str(int(int(invoice['valueMsat'])/1000))))
                                except:
                                    pass

                                if PUSHOVER_ENABLE and pushover_message:
                                    crl = pycurl.Curl()
                                    crl.setopt(crl.URL, 'https://api.pushover.net/1/messages.json')
                                    crl.setopt(pycurl.HTTPHEADER, ['Content-Type: application/json' , 'Accept: application/json'])
                                    data = json.dumps({"token": PUSHOVER_API_TOKEN, "user": PUSHOVER_USER_TOKEN, "title": "RSS Notifier", "message": pushover_message })
                                    crl.setopt(pycurl.POST, 1)
                                    crl.setopt(pycurl.POSTFIELDS, data)
                                    crl.perform()
                                    crl.close()

            invoice_database_index += 1
# End For Loop

    if JSON_BOOSTAGRAMS:
        if boostagram_json_data is not None:
            # Open and read in previous index position
            boostagramfilereaddata = []
            boostagramfilewritedata = []
            boostagramfilename = os.path.expanduser('/home/extractlv/settings/boostagrams.json')
            if os.path.exists(boostagramfilename):
                with open(boostagramfilename) as boostagramfileread:
                    try:
                        boostagramfilereaddata = json.load(boostagramfileread)
                    except:
                        pass
                boostagramfileread.close()

#            print('Read File Data: {value}'.format(value=boostagramfilereaddata))
#            print('New Boosta Data: {value}'.format(value=boostagram_json_data))
            if boostagramfilereaddata:
#                print('Read File Data')
                boostagramfilewritedata = boostagramfilereaddata + (boostagram_json_data)
            else:
#                print('No File Data')
                boostagramfilewritedata = boostagram_json_data
            if boostagramfilewritedata:
                with open(boostagramfilename, 'w') as boostagramfile:
                    json.dump(boostagramfilewritedata, boostagramfile)
#                    print('Written File Data: {value}'.format(value=boostagramfilewritedata))
                boostagramfile.close()

    if EMAIL_ENABLE:
        invoice_count = 0
        if len(dictionary_invoice_list):
            for invoice in dictionary_invoice_list['invoices']:
                if "amtPaidSat" in invoice and "isKeysend" in invoice:
                    print('Time: {value1} Amount: {value2}'.format(value1=datetime.utcfromtimestamp(int(invoice['creationDate'])), value2=invoice['amtPaidSat']))

                invoice_count += 1
# End For Loop

        emailsubject = "NO"
        emailcontent = "YES"
        #Create Headers
        headers = ["From: " + EMAIL_ACCOUNT_USERNAME, "Subject: " + emailsubject, "To: " + EMAIL_SEND_TO_ADDRESS, "MIME-Version: 1.0", "Content-Type: text/html"]
        headers = "\r\n".join(headers)

        #Connect to Email Server
        emailsession = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        emailsession.ehlo()
        emailsession.starttls()
        emailsession.ehlo()

        #Login to Gmail
        emailsession.login(EMAIL_ACCOUNT_USERNAME, EMAIL_ACCOUNT_PASSWORD)

        #Send Email & Exit
        emailsession.sendmail(EMAIL_ACCOUNT_USERNAME, EMAIL_SEND_TO_ADDRESS, headers + "\r\n\r\n" + emailcontent)
        emailsession.quit

# Boilerplate for flagging current database index for reduce seek times in future TBD
    # Check for new Invoices
    # Write all previous and current Invoices
    # Open and read in previous index position
    invoicefile = os.path.expanduser('/home/extractlv/settings/cacheposition.db')
    with open(invoicefile, 'w') as invoicefiledata:
        invoicefiledata.write(str(invoice_database_index))

# End

if __name__ == "__main__":
    main()
# End
