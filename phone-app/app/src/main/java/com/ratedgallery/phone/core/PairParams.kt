package com.ratedgallery.phone.core

import java.net.URLDecoder

/** Host, port and one-time code needed to pair with the PC (API.md section 2). */
data class PairParams(val host: String, val port: Int, val code: String) {
    companion object {
        const val SCHEME = "ratedgallery"
        const val LINK_HOST = "pair"

        private val HOST_RE = Regex("^[A-Za-z0-9.-]{1,253}$")
        private val IPV6_RE = Regex("^\\[?[0-9A-Fa-f:.]{2,45}]?$")
        private val CODE_RE = Regex("^[0-9]{6}$")

        /**
         * Parses `ratedgallery://pair?host=..&port=..&code=..`.
         * Returns null if the link is not a valid pairing link.
         */
        fun parseLink(link: String?): PairParams? {
            if (link.isNullOrBlank()) return null
            val trimmed = link.trim()
            val schemeEnd = trimmed.indexOf("://")
            if (schemeEnd <= 0) return null
            if (!trimmed.substring(0, schemeEnd).equals(SCHEME, ignoreCase = true)) return null
            val rest = trimmed.substring(schemeEnd + 3)
            val q = rest.indexOf('?')
            if (q < 0) return null
            val authority = rest.substring(0, q).trimEnd('/')
            if (!authority.equals(LINK_HOST, ignoreCase = true)) return null
            val query = rest.substring(q + 1).substringBefore('#')
            val params = HashMap<String, String>()
            for (part in query.split('&')) {
                if (part.isEmpty()) continue
                val eq = part.indexOf('=')
                val key = if (eq >= 0) part.substring(0, eq) else part
                val value = if (eq >= 0) part.substring(eq + 1) else ""
                val k = decode(key) ?: return null
                val v = decode(value) ?: return null
                params.putIfAbsent(k, v)
            }
            return validate(params["host"], params["port"], params["code"])
        }

        /** Validates manual input (or link values). Returns null if anything is invalid. */
        fun validate(host: String?, port: String?, code: String?): PairParams? {
            val h = normalizeHost(host) ?: return null
            val p = port?.trim()?.toIntOrNull() ?: return null
            if (p !in 1..65535) return null
            val c = code?.trim()?.replace(" ", "") ?: return null
            if (!CODE_RE.matches(c)) return null
            return PairParams(h, p, c)
        }

        /** Returns the cleaned host, or null if it is not a plausible host name or IP address. */
        fun normalizeHost(host: String?): String? {
            val h = host?.trim() ?: return null
            if (h.isEmpty()) return null
            if (h.contains(':')) {
                if (!IPV6_RE.matches(h)) return null
                return h.removePrefix("[").removeSuffix("]")
            }
            if (!HOST_RE.matches(h) || h.startsWith('.') || h.startsWith('-')) return null
            return h
        }

        private fun decode(s: String): String? = try {
            URLDecoder.decode(s, "UTF-8")
        } catch (e: IllegalArgumentException) {
            null
        }
    }
}
