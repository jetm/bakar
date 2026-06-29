# Host-mode build isolation: rpm-native runs on the build host. Its
# %__transaction_* macros point each transaction plugin at
# %{_libdir}/rpm-plugins, which during a target transaction resolves to the
# absolute host path /usr/lib/rpm-plugins. On a build host that has its own rpm
# installed (e.g. Arch/CachyOS rpm 6.x while this rpm-native is 4.19) dnf's
# do_rootfs aborts with:
#     Failed to dlopen /usr/lib/rpm-plugins/audit.so: undefined symbol: rpmteVfyLevel
# because the host plugin is ABI-incompatible with rpm-native's librpm. None of
# the transaction plugins are needed for offline cross image assembly, so
# disable them at their source: the macros file rpm reads. This mirrors how the
# rpm recipe itself appends to ${libdir}/rpm/macros in its own do_install.
do_install:append:class-native() {
	cat >> ${D}${libdir}/rpm/macros <<'EOF'

# bakar host-mode: disable rpm transaction plugins (build-host rpm-plugins leak guard)
%__transaction_systemd_inhibit %{nil}
%__transaction_selinux         %{nil}
%__transaction_syslog          %{nil}
%__transaction_ima             %{nil}
%__transaction_fapolicyd       %{nil}
%__transaction_fsverity        %{nil}
%__transaction_prioreset       %{nil}
%__transaction_audit           %{nil}
%__transaction_dbus_announce   %{nil}
EOF
}
