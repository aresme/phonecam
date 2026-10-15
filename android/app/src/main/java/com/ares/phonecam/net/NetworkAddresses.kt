package com.ares.phonecam.net

import java.net.Inet4Address
import java.net.NetworkInterface

/** A local IPv4 address the PC can reach the phone on. */
data class LocalAddress(val ip: String, val iface: String) {
    val kind: String get() = when {
        iface.startsWith("ap") || iface.startsWith("swlan") || iface.startsWith("wlan1") -> "Hotspot"
        iface.startsWith("wlan") -> "Wi-Fi"
        iface.startsWith("rndis") || iface.startsWith("usb") || iface.startsWith("ncm") -> "USB tethering"
        iface.startsWith("eth") -> "Ethernet"
        else -> iface
    }
}

object NetworkAddresses {
    /** Cellular, VPN and virtual interfaces are not reachable from the LAN. */
    private val excluded = listOf("lo", "rmnet", "ccmni", "dummy", "tun", "ppp", "ipsec", "clat", "v4-")

    fun list(): List<LocalAddress> = try {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback && excluded.none { p -> it.name.startsWith(p) } }
            .flatMap { ni ->
                ni.inetAddresses.toList().filterIsInstance<Inet4Address>()
                    .filter { !it.isLinkLocalAddress }
                    .map { LocalAddress(it.hostAddress ?: "", ni.name) }
            }
            .filter { it.ip.isNotEmpty() }
            .sortedBy { if (it.kind == "Wi-Fi") 0 else 1 }
    } catch (_: Exception) {
        emptyList()
    }
}
