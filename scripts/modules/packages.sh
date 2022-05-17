sudo apt update
sudo apt dist-upgrade -y
sudo apt install docker.io docker-compose git -y

# install
# nix

# setup docker permissions


function install_e2guardian() {
    git clone https://github.com/e2guardian/e2guardian.git
    ./autogen.sh && ./configure  '--prefix=/usr' '--enabl
e-clamd=yes' '--with-proxyuser=e2guardian' '--with-proxygrou
p=e2guardian' '--sysconfdir=/etc' '--localstatedir=/var' '--
enable-icap=yes' '--enable-commandline=yes' '--enable-email=
yes' '--enable-ntlm=yes' '--mandir=${prefix}/share/man' '--i
nfodir=${prefix}/share/info' '--enable-pcre=yes' '--enable-s
slmitm=yes' 'CPPFLAGS=-mno-sse2 -g -O2' && make
sudo make install
}
