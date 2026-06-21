package com.veer.wheel

import android.annotation.SuppressLint
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.view.View
import android.view.WindowInsetsController
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.PopupMenu
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.Locale
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Veer Wheel — телефон как руль.
 * Наклон телефона (как настоящий руль) → ось руля.
 * Аналоговые педали ГАЗ / ТОРМОЗ (сила нажатия по высоте пальца).
 * Данные шлются по UDP на ПК, где их принимает pc/wheel.py.
 */
class MainActivity : AppCompatActivity(), SensorEventListener {

    private lateinit var sensorManager: SensorManager
    private var accel: Sensor? = null

    private val gravity = FloatArray(3)
    private val alpha = 0.25f

    // 270° полного хода: ±135° от центра.
    private val maxAngle = (135f * PI / 180f).toFloat()
    private val piFloat = PI.toFloat()
    private val twoPiFloat = (2 * PI).toFloat()

    @Volatile private var steer = 0f
    @Volatile private var gas = 0f
    @Volatile private var brake = 0f
    @Volatile private var btnA = 0
    @Volatile private var btnB = 0

    @Volatile private var centerOffset = 0f
    private var currentRoll = 0f

    @Volatile private var running = true
    @Volatile private var targetIp = "192.168.1.100"
    @Volatile private var targetPort = 5555

    private val discoveryPort = 5556

    private lateinit var connectivityManager: ConnectivityManager
    private lateinit var statusView: TextView
    private lateinit var wheelView: WheelView
    private lateinit var gasPedal: PedalView
    private lateinit var brakePedal: PedalView
    private lateinit var vibrator: Vibrator
    private var sender: Thread? = null

    // Поля диалога подключения (могут быть null, если диалог закрыт).
    private var ipField: EditText? = null
    private var dialogStatus: TextView? = null

    @Volatile private var rumbleStrength = 0f
    @Volatile private var rumbleDeadlineMs = 0L
    @Volatile private var lastAckMs = 0L
    private var lastVibrateMs = 0L

    @Volatile private var wifiLocalIp = ""
    @Volatile private var connected = false

    @SuppressLint("ClickableViewAccessibility", "SetTextI18n")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemBars()

        sensorManager = getSystemService(SENSOR_SERVICE) as SensorManager
        accel = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        connectivityManager = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        }

        statusView = findViewById(R.id.status)
        wheelView = findViewById(R.id.wheel)
        gasPedal = (findViewById<View>(R.id.gasPedal) as PedalView).apply { type = PedalView.Type.GAS }
        brakePedal = (findViewById<View>(R.id.brakePedal) as PedalView).apply { type = PedalView.Type.BRAKE }

        // Контекстное меню на кнопку «Назад».
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                PopupMenu(this@MainActivity, statusView).apply {
                    menu.add(0, 1, 0, "⚙ Подключение")
                    menu.add(0, 2, 0, "Центр")
                    menu.add(0, 3, 0, "Выход")
                    setOnMenuItemClickListener { item ->
                        when (item.itemId) {
                            1 -> { showConnectDialog(); true }
                            2 -> {
                                centerOffset = currentRoll
                                statusView.text = "центр установлен"
                                true
                            }
                            3 -> { finishAffinity(); true }
                            else -> false
                        }
                    }
                    show()
                }
            }
        })

        gasPedal.setOnPedalChangeListener { v -> gas = v }
        brakePedal.setOnPedalChangeListener { v -> brake = v }

        wifiLocalIp = getWifiLocalIp() ?: ""
        startSender()
        discoverPc(silent = true)
    }

    @SuppressLint("InflateParams")
    private fun showConnectDialog() {
        val view = layoutInflater.inflate(R.layout.dialog_connect, null)
        val ipField = view.findViewById<EditText>(R.id.ipField)
        val portField = view.findViewById<EditText>(R.id.portField)
        val dialogStatus = view.findViewById<TextView>(R.id.dialogStatus)
        this.ipField = ipField
        this.dialogStatus = dialogStatus

        ipField.setText(targetIp)
        portField.setText(targetPort.toString())
        dialogStatus.text = if (connected) "подключено · $targetIp:$targetPort" else "не подключено"

        view.findViewById<Button>(R.id.findBtn).setOnClickListener {
            wifiLocalIp = getWifiLocalIp() ?: ""
            dialogStatus.text = "поиск ПК…"
            discoverPc()
        }

        val dialog = androidx.appcompat.app.AlertDialog.Builder(this, R.style.Theme_VeerWheel)
            .setView(view)
            .setPositiveButton("Подключить") { _, _ ->
                val typedIp = ipField.text.toString().trim()
                targetIp = typedIp.ifBlank { targetIp }
                targetPort = portField.text.toString().trim().toIntOrNull() ?: 5555
                statusView.text = "→ $targetIp:$targetPort"
            }
            .setNegativeButton("Закрыть", null)
            .create()
        dialog.show()
    }

    private fun hideSystemBars() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.insetsController?.apply {
                hide(android.view.WindowInsets.Type.statusBars() or android.view.WindowInsets.Type.navigationBars())
                systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                View.SYSTEM_UI_FLAG_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            )
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemBars()
    }

    private fun isOnWifi(): Boolean {
        val network = connectivityManager.activeNetwork ?: return false
        val caps = connectivityManager.getNetworkCapabilities(network) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
    }

    private fun getWifiLocalIp(): String? {
        val network = connectivityManager.activeNetwork ?: return null
        val caps = connectivityManager.getNetworkCapabilities(network) ?: return null
        if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return null
        val props = connectivityManager.getLinkProperties(network) ?: return null
        for (addr in props.linkAddresses) {
            if (addr.address is Inet4Address && !addr.address.isLoopbackAddress) {
                return addr.address.hostAddress
            }
        }
        return null
    }

    private fun computeBroadcastAddresses(): List<String> {
        val result = mutableListOf<String>()
        val network = connectivityManager.activeNetwork ?: return result
        val properties = connectivityManager.getLinkProperties(network) ?: return result
        for (addr in properties.linkAddresses) {
            val ip = addr.address
            if (ip !is Inet4Address || ip.isLoopbackAddress) continue
            val prefixLen = addr.prefixLength
            if (prefixLen <= 0 || prefixLen >= 32) continue
            val ipBytes = ip.address
            var ipInt = 0
            for (i in 0..3) {
                ipInt = (ipInt shl 8) or (ipBytes[i].toInt() and 0xFF)
            }
            val mask = (-1 shl (32 - prefixLen))
            val broadcastInt = ipInt or mask.inv()
            val b0 = (broadcastInt ushr 24) and 0xFF
            val b1 = (broadcastInt ushr 16) and 0xFF
            val b2 = (broadcastInt ushr 8) and 0xFF
            val b3 = broadcastInt and 0xFF
            result.add("$b0.$b1.$b2.$b3")
        }
        return result
    }

    private fun applyTargetIp(ip: String, message: String) {
        targetIp = ip
        runOnUiThread {
            ipField?.setText(ip)
            statusView.text = message
            dialogStatus?.text = message
        }
    }

    @SuppressLint("SetTextI18n")
    private fun discoverPc(silent: Boolean = false) {
        Thread {
            try {
                if (!isOnWifi()) {
                    if (!silent) runOnUiThread {
                        statusView.text = "нет Wi-Fi — подключись к сети"
                        dialogStatus?.text = "нет Wi-Fi"
                    }
                    return@Thread
                }

                val network = connectivityManager.activeNetwork
                val linkProps = connectivityManager.getLinkProperties(network)
                var gatewayIp: String? = null
                if (linkProps != null) {
                    for (route in linkProps.routes) {
                        if (route.isDefaultRoute) {
                            gatewayIp = route.gateway?.hostAddress
                            if (!gatewayIp.isNullOrBlank()) break
                        }
                    }
                }

                if (!gatewayIp.isNullOrBlank()) {
                    applyTargetIp(gatewayIp, "шлюз $gatewayIp")
                    if (probePc(gatewayIp) == ProbeResult.FOUND) {
                        applyTargetIp(gatewayIp, "найден $gatewayIp")
                        return@Thread
                    }
                }

                DatagramSocket().use { s ->
                    s.broadcast = true
                    s.soTimeout = 1500
                    val msg = "VEER_DISCOVER".toByteArray()

                    val computed = computeBroadcastAddresses()
                    val fallback = listOf("255.255.255.255", "192.168.0.255", "192.168.1.255", "10.0.0.255")
                    val targets = (computed + fallback).distinct()

                    for (target in targets) {
                        try {
                            s.send(DatagramPacket(msg, msg.size,
                                InetAddress.getByName(target), discoveryPort))
                        } catch (_: Exception) {
                        }
                    }

                    val buf = ByteArray(64)
                    val resp = DatagramPacket(buf, buf.size)
                    s.receive(resp)
                    val ip = resp.address.hostAddress ?: return@use
                    applyTargetIp(ip, "найден $ip")
                }
            } catch (_: SocketTimeoutException) {
                if (!silent) runOnUiThread {
                    statusView.text = "ПК не найден — wheel.py запущен?"
                    dialogStatus?.text = "ПК не найден — wheel.py запущен?"
                }
            } catch (_: Exception) {
                if (!silent) runOnUiThread {
                    statusView.text = "ошибка поиска"
                    dialogStatus?.text = "ошибка поиска"
                }
            }
        }.start()
    }

    private enum class ProbeResult { FOUND, NOT_FOUND }

    private fun probePc(ip: String): ProbeResult {
        return try {
            DatagramSocket().use { s ->
                s.soTimeout = 1000
                val msg = "VEER_DISCOVER".toByteArray()
                s.send(DatagramPacket(msg, msg.size, InetAddress.getByName(ip), discoveryPort))

                val buf = ByteArray(64)
                val resp = DatagramPacket(buf, buf.size)
                s.receive(resp)
                val line = String(resp.data, 0, resp.length).trim()
                if (line == "VEER_HERE" || line.startsWith("A1")) ProbeResult.FOUND
                else ProbeResult.NOT_FOUND
            }
        } catch (_: Exception) {
            ProbeResult.NOT_FOUND
        }
    }

    private fun clamp(v: Float, lo: Float, hi: Float): Float = max(lo, min(hi, v))

    private fun normalizeAngle(delta: Float): Float {
        var value = delta
        while (value > piFloat) value -= twoPiFloat
        while (value < -piFloat) value += twoPiFloat
        return value
    }

    private fun readRumblePackets(socket: DatagramSocket) {
        while (running) {
            try {
                val buf = ByteArray(64)
                val packet = DatagramPacket(buf, buf.size)
                socket.receive(packet)
                val line = String(packet.data, 0, packet.length).trim().lineSequence().lastOrNull()
                    ?: continue
                val now = System.currentTimeMillis()
                lastAckMs = now

                if (line == "A1" || line.startsWith("A1,")) {
                    continue
                }
                if (!line.startsWith("R1,")) continue

                val value = line.substringAfter(',').toFloatOrNull() ?: continue
                rumbleStrength = clamp(value, 0f, 1f)
                rumbleDeadlineMs = System.currentTimeMillis() + 300L
            } catch (_: SocketTimeoutException) {
                break
            } catch (_: Exception) {
                break
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun updateVibration() {
        val now = System.currentTimeMillis()
        val strength = rumbleStrength
        if (strength <= 0.01f || now > rumbleDeadlineMs) {
            rumbleStrength = 0f
            if (lastVibrateMs != 0L) {
                vibrator.cancel()
                lastVibrateMs = 0L
            }
            return
        }

        if (now - lastVibrateMs < 45L) return

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val amplitude = (1 + clamp(strength, 0f, 1f) * 254).roundToInt()
            vibrator.vibrate(VibrationEffect.createOneShot(90L, amplitude))
        } else {
            vibrator.vibrate((35f + clamp(strength, 0f, 1f) * 55f).roundToInt().toLong())
        }
        lastVibrateMs = now
    }

    @SuppressLint("SetTextI18n")
    private fun startSender() {
        sender = Thread {
            val localIp = wifiLocalIp
            val socket = if (localIp.isNotEmpty()) {
                try { DatagramSocket(InetSocketAddress(localIp, 0)) }
                catch (_: Exception) { DatagramSocket() }
            } else {
                DatagramSocket()
            }
            socket.soTimeout = 1
            var tick = 0
            while (running) {
                try {
                    val addr = InetAddress.getByName(targetIp)
                    // Locale.US обязательно: иначе в ru_RU дроби через запятую
                    // (0,000) и split(',') в приёмнике ломает протокол.
                    val msg = String.format(
                        Locale.US, "V1,%.4f,%.3f,%.3f,%d,%d\n",
                        steer, gas, brake, btnA, btnB
                    )
                    val bytes = msg.toByteArray()
                    socket.send(DatagramPacket(bytes, bytes.size, addr, targetPort))
                } catch (_: Exception) {
                    // Сеть может временно отсутствовать — просто повторяем.
                }
                readRumblePackets(socket)
                updateVibration()

                // Обновляем визуал руля и статус ~10 раз/сек.
                if (tick++ % 6 == 0) {
                    val now = System.currentTimeMillis()
                    connected = now - lastAckMs < 1500L
                    runOnUiThread {
                        statusView.text = if (connected) "подключено" else "поиск…"
                    }
                }
                try {
                    Thread.sleep(10) // ~100 Гц
                } catch (_: InterruptedException) {
                    break
                }
            }
            socket.close()
            vibrator.cancel()
        }.also { it.start() }
    }

    override fun onResume() {
        super.onResume()
        accel?.let {
            sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME)
        }
    }

    override fun onPause() {
        super.onPause()
        sensorManager.unregisterListener(this)
        vibrator.cancel()
    }

    override fun onDestroy() {
        running = false
        sender?.interrupt()
        super.onDestroy()
    }

    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type != Sensor.TYPE_ACCELEROMETER) return
        gravity[0] = alpha * event.values[0] + (1 - alpha) * gravity[0]
        gravity[1] = alpha * event.values[1] + (1 - alpha) * gravity[1]

        currentRoll = atan2(gravity[0], gravity[1])
        val r = normalizeAngle(currentRoll - centerOffset)
        steer = max(-1f, min(1f, r / maxAngle))
        // Визуал руля — на каждом событии сенсора (~50–100 Гц), плавно.
        wheelView.setSteer(steer)
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
}
