#!/usr/bin/env bash
NEW_USER=ruchniux


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

# Create a new sudo user to be able to revert this install

# setup docker permissions
#

update sudoers file so custom apt can be run
