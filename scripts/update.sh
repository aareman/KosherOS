#!/usr/bin/env bash

git -C /root/kosher-linux pull
cat /root/kosher-linux/lists/main.list > /etc/hosts
