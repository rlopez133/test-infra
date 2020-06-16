set -euo pipefail

function version_is_greater() {
    if [ "$(printf '%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; then
        return
    fi
    echo $(printf '%s\n' "$2" "$1" | sort -V | head -n1)
    false
}


function install_libvirt() {
  echo "Installing libvirt..."
  sudo dnf install -y libvirt libvirt-devel libvirt-daemon-kvm qemu-kvm
  sudo systemctl enable --now libvirtd

  current_version="$(libvirtd --version | awk '{print $3}')"
  minimum_version="5.5.100"

  sudo sed -i -e 's/#listen_tls/listen_tls/g' /etc/libvirt/libvirtd.conf
  sudo sed -i -e 's/#listen_tcp/listen_tcp/g' /etc/libvirt/libvirtd.conf
  sudo sed -i -e 's/#auth_tcp = "sasl"/auth_tcp = "none"/g' /etc/libvirt/libvirtd.conf
  sudo sed -i -e 's/#tcp_port/tcp_port/g' /etc/libvirt/libvirtd.conf
  sudo sed -i -e 's/#security_driver = "selinux"/security_driver = "none"/g' /etc/libvirt/qemu.conf
  if ! version_is_greater "$current_version" "$minimum_version"; then
    sudo sed -i -e 's/#LIBVIRTD_ARGS="--listen"/LIBVIRTD_ARGS="--listen"/g' /etc/sysconfig/libvirtd
    sudo systemctl restart libvirtd
  else
    echo "libvirtd version is greater then 5.5.x, starting libvirtd-tcp.socket"
    sudo systemctl stop libvirtd
    sudo systemctl restart libvirtd.socket
    sudo systemctl enable --now libvirtd-tcp.socket
    sudo systemctl start libvirtd-tcp.socket
    sudo systemctl start libvirtd
  fi
  current_user=$(whoami)
  sudo gpasswd -a $current_user libvirt
  sudo gpasswd -a $current_user qemu

  #echo "$current_user ALL=(ALL:ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/$current_user
}

function install_runtime_container() {
  if ! [ -x "$(command -v docker)" ] && ! [ -x "$(command -v podman)" ]; then
    sudo dnf install podman -y
  elif [ -x "$(command -v podman)" ]; then
    current_version="$(podman -v | awk '{print $3}')"
    minimum_version="1.6.4"
    if ! version_is_greater "$current_version" "$minimum_version"; then
      sudo dnf install podman-$minimum_version  -y
    fi
  else
    echo "docker or podman is already installed"
  fi
}

function install_packages(){
  sudo dnf install -y make python3 git jq bash-completion xinetd
  sudo systemctl enable --now xinetd
}

function install_skipper() {
   pip3 install strato-skipper --user
   echo "export PATH=~/.local/bin:$PATH" >> ~/.bashrc
   export PATH="$PATH:~/.local/bin"
}

function config_firewalld() {
  echo "Config firewall"
  sudo systemctl start firewalld
  sudo firewall-cmd --zone=public --permanent --add-port=6000/tcp
  sudo firewall-cmd --zone=libvirt --permanent --add-port=6000/tcp
  sudo firewall-cmd --zone=public --permanent --add-port=6008/tcp
  sudo firewall-cmd --zone=libvirt --permanent --add-port=6008/tcp
  sudo firewall-cmd --reload
  sudo systemctl restart libvirtd
}

install_packages
install_libvirt
install_runtime_container
install_skipper
config_firewalld
touch ~/.gitconfig
sudo chmod ugo+rx "$(dirname "$(pwd)")"
sudo setenforce 0
