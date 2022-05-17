#!/usr/bin/env bash
_user=ruchniux
_currentUser="$USER"


# TODO: a module system to keep it clean and scalable
# modules=(
#     "scripts/modules/packages.sh"
#     "scripts/modules/create-user.sh"
#     # "scripts/modules/configure-crons.sh"
#     # "scripts/modules/configure-hosts.sh"
#     # "scripts/modules/setup-user-permissions.sh"
# )
#
# for m in "${modules[@]}"; do
#     ./$m
# done


# Packages
sudo apt update
sudo apt upgrade -y
sudo apt install docker.io docker-compose git -y

# install nix
sudo mkdir /nix
sudo chown "$_currentUser"
sh <(curl -L https://nixos.org/nix/install) --no-daemon

# Create a new sudo user to be able to revert this install

# setup docker permissions

# NOT DOING: just removing sudo and sticking with nix
# update sudoers file so custom apt can be run


# TODO: build list
# - grab from these lists
# HOST FILES
https://raw.githubusercontent.com/anudeepND/blacklist/master/facebook.txt
https://zerodot1.gitlab.io/CoinBlockerLists/hosts_browser
# NEED HOST PREFIX
https://raw.githubusercontent.com/chadmayfield/my-pihole-blocklists/master/lists/pi_blocklist_porn_top1m.list

# TODO: Lock lists, waiting on working concept. This is the nsswitch thing

function install_e2guardian() {
    # clone project
    git clone https://github.com/e2guardian/e2guardian.git
    cd e2guardian
    # install dependencies
    sudo apt update && sudo apt install libtommath1 libevent-pthreads-2.1-6
    sudo service e2guardian status
    # configuration
    # As sudo
    mkdir -p /etc/e2guardian/ssl/generatedcerts
    chown -R e2guardian. /etc/e2guardian/ssl
    cd /etc/e2guardian/ssl
    openssl genrsa 4096 > private_root.pem
    openssl req -new -x509 -days 3650 -key private_root.pem -out my_rootCA.crt
    # server needs to be the actual name of the server (here is 0.0.0.0)
    openssl genrsa > private_cert.pem
    chown -R e2guardian. /etc/e2guardian/ssl

    # TODO: backup /etc/e2guardian

}

# Custom boot splash screen
# https://askubuntu.com/a/2292
