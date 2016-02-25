#!/bin/bash
username=mini-mon
password=password
monasca_host=10.12.4.60
freq=$1

sudo monasca-setup -u $username -p $password --keystone_url http://$monasca_host:5000/v3 --monasca_url http://$monasca_host:8070/v2.0 --system_only --log_level INFO --hostname virtualbox.tinypony.comptel.com --check_frequency $freq
