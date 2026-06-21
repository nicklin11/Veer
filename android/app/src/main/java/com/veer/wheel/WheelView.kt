package com.veer.wheel

import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View

/**
 * Визуальный руль: реалистичное кольцо со спицами и хабом,
 * повёрнутое на угол наклона телефона. steer — нормализованное -1..1.
 * Обновляется из потока сенсора (через UI-thread post), поэтому плавно.
 */
class WheelView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(context, attrs, defStyle) {

    @Volatile private var steer = 0f
    private val maxRotationDeg = 135f

    private val rimPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 30f
        strokeCap = Paint.Cap.ROUND
    }
    private val rimHiPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 6f
        color = 0x55FFFFFF
        strokeCap = Paint.Cap.ROUND
    }
    private val rimShadowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 34f
        color = 0x44000000
    }
    private val spokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 18f
        color = 0xFF23282E.toInt()
        strokeCap = Paint.Cap.ROUND
    }
    private val spokeHiPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 4f
        color = 0x33FFFFFF
        strokeCap = Paint.Cap.ROUND
    }
    private val hubPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val hubRingPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 5f
        color = 0xFF11151A.toInt()
    }
    private val centerDotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = 0xFFE6B450.toInt()
    }
    private val topMarkPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = 0xFFE6B450.toInt()
    }
    private val centerRefPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = 0x9933CC66.toInt()
    }
    private val rect = RectF()
    private val spokePath = Path()

    fun setSteer(value: Float) {
        // Инвертируем: наклон телефона влево = руль влево на экране
        val clamped = (-value).coerceIn(-1f, 1f)
        if (kotlin.math.abs(clamped - steer) > 0.0005f) {
            steer = clamped
            invalidateOnUi()
        }
    }

    private fun invalidateOnUi() {
        if (android.os.Looper.myLooper() === android.os.Looper.getMainLooper()) {
            invalidate()
        } else {
            postInvalidateOnAnimation()
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val cx = width / 2f
        val cy = height / 2f
        val radius = (minOf(width, height) / 2f) * 0.84f - rimPaint.strokeWidth / 2f
        rect[cx - radius, cy - radius, cx + radius] = cy + radius

        // Ориентир «прямо» на 12 часов.
        canvas.drawCircle(cx, cy - radius - 14f, 6f, centerRefPaint)

        val rotation = steer * maxRotationDeg
        canvas.save()
        canvas.rotate(rotation, cx, cy)

        // Тень обода (снизу).
        canvas.drawCircle(cx, cy + 4f, radius, rimShadowPaint)

        // Обод — металлик.
        rimPaint.shader = LinearGradient(
            cx - radius, cy - radius, cx + radius, cy + radius,
            0xFF4A525C.toInt(), 0xFF1B1F24.toInt(), Shader.TileMode.CLAMP
        )
        canvas.drawCircle(cx, cy, radius, rimPaint)
        // Блик на ободе (верхняя треть).
        rimHiPaint.shader = LinearGradient(
            cx, cy - radius, cx, cy,
            0x66FFFFFF.toInt(), 0x00FFFFFF.toInt(), Shader.TileMode.CLAMP
        )
        canvas.drawArc(rect, -150f, 120f, false, rimHiPaint)

        // Спицы: три луча из центра к ободу.
        spokePath.reset()
        val spokeEnd = radius - rimPaint.strokeWidth / 2f - 2f
        val angles = floatArrayOf(-90f, 30f, 150f) // вверх, вниз-вправо, вниз-влево
        for (a in angles) {
            val rad = Math.toRadians(a.toDouble())
            val ex = (cx + spokeEnd * Math.cos(rad)).toFloat()
            val ey = (cy + spokeEnd * Math.sin(rad)).toFloat()
            spokePath.moveTo(cx, cy)
            spokePath.lineTo(ex, ey)
        }
        canvas.drawPath(spokePath, spokePaint)
        canvas.drawPath(spokePath, spokeHiPaint)

        // Хаб.
        val hubR = radius * 0.28f
        hubPaint.shader = RadialGradient(
            cx - hubR * 0.3f, cy - hubR * 0.3f, hubR * 1.4f,
            0xFF333A42.toInt(), 0xFF12151A.toInt(), Shader.TileMode.CLAMP
        )
        canvas.drawCircle(cx, cy, hubR, hubPaint)
        canvas.drawCircle(cx, cy, hubR, hubRingPaint)

        // Центральная точка + верхняя метка на ободе.
        canvas.drawCircle(cx, cy, hubR * 0.32f, centerDotPaint)
        canvas.drawCircle(cx, cy - radius + rimPaint.strokeWidth / 2f + 4f, 8f, topMarkPaint)

        canvas.restore()
    }
}
