#!/usr/bin/env bash
_user=ruchniux
_currentUser="$USER"

# TODO: add password type (wife/spouse, chetzy chetzy)
# TODO: add validation for this
read -p "Enter in Admin Password: " _ruchniuxPassword
PS3="Select Filter Type: "
# TODO: keep choosing if not valid choice
select filterType in whitelist proxy; do
_filterType=$filterType
break
done

echo "USER: ${_currentUser}"
echo "filter: ${_filterType}"

echo "adminPAssword: ${_ruchniuxPassword}"
exit


# STEPS
# - get user input
# - create ruchniux user
# - install application under that user
# - setup crons for system updates (optionaly allow sudo just for this)
# - install all system deps and config
# - hand control over to that user
# - bios boot cleanup (block boot from usb)


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

function _install.docker(){
    sudo apt install docker.io docker-compose -y
    # setup docker permissions

}


function _install.nix(){
sudo mkdir /nix
sudo chown "$_currentUser"
sh <(curl -L https://nixos.org/nix/install) --no-daemon
}
function _install.whitelist(){
    # TODO: implement
}
function _install.e2guardian() {
    # TODO: refactor this into helper functions
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
    # TODO: generate this headless
    openssl req -new -x509 -days 3650 -key private_root.pem -out my_rootCA.crt
    # server needs to be the actual name of the server (here is 127.0.0.1)
    openssl genrsa > private_cert.pem
    chown -R e2guardian. /etc/e2guardian/ssl

    # Linking
    ln -s e2guardian /etc/
    ln -s e2guardian/languages /usr/share/e2guardian

}

function install_and_update() {
sudo apt update
sudo apt upgrade -y
sudo apt install git -y
_install.docker
_install.nix
# TODO: make this conditional based on filter type
_install.e2guardian
}




# Create a new sudo user to be able to revert this install


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



# TODO: Custom boot splash screen
# https://askubuntu.com/a/2292
# TODO: custom wallpaper
