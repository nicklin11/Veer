package com.veer.wheel

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View

/**
 * Аналоговая педаль: сила нажатия (0..1) определяется позицией пальца по высоте.
 * Верх = 0, низ = 1. Рисуется как вертикальная шкала с заполняющимся столбцом.
 * type = GAS (зелёный) или BRAKE (красный).
 */
class PedalView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(context, attrs, defStyle) {

    enum class Type { GAS, BRAKE }

    var type: Type = Type.GAS
        set(v) { field = v; invalidate() }

    /** 0..1 — текущая сила нажатия. */
    var value: Float = 0f
        private set

    private var listener: ((Float) -> Unit)? = null

    private val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val trackPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 3f
        color = 0x33FFFFFF
    }
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = 28f
        textAlign = Paint.Align.CENTER
        isFakeBoldText = true
        alpha = 230
    }
    private val pctPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = 18f
        textAlign = Paint.Align.CENTER
        alpha = 180
    }
    private val rect = RectF()

    fun setOnPedalChangeListener(l: (Float) -> Unit) { listener = l }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        val (c1, c2) = when (type) {
            Type.GAS -> 0xFF1F6B43.toInt() to 0xFF33CC66.toInt()
            Type.BRAKE -> 0xFF8A2424.toInt() to 0xFFE25050.toInt()
        }
        bgPaint.shader = LinearGradient(0f, 0f, 0f, h.toFloat(),
            0xFF15191E.toInt(), 0xFF0C0F12.toInt(), Shader.TileMode.CLAMP)
        fillPaint.shader = LinearGradient(0f, h.toFloat(), 0f, 0f, c1, c2, Shader.TileMode.CLAMP)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        val pad = 16f
        rect[pad, pad, w - pad] = h - pad

        canvas.drawRoundRect(rect, 18f, 18f, bgPaint)

        // Заполнение снизу вверх на величину value.
        val fillH = (rect.height()) * value
        rect.top = h - pad - fillH
        canvas.drawRoundRect(rect, 18f, 18f, fillPaint)

        // Контур.
        rect[pad, pad, w - pad] = h - pad
        canvas.drawRoundRect(rect, 18f, 18f, trackPaint)

        // Подпись.
        val label = if (type == Type.GAS) "ГАЗ" else "ТОРМОЗ"
        val baseline = h / 2f - (labelPaint.ascent() + labelPaint.descent()) / 2f
        labelPaint.alpha = if (value > 0.5f) 255 else 230
        canvas.drawText(label, w / 2f, baseline, labelPaint)
        canvas.drawText("${(value * 100).toInt()}%", w / 2f, baseline + 34f, pctPaint)
    }

    override fun onTouchEvent(ev: MotionEvent): Boolean {
        when (ev.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE -> {
                value = (1f - (ev.y / height)).coerceIn(0f, 1f)
                listener?.invoke(value)
                invalidate()
                return true
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                value = 0f
                listener?.invoke(0f)
                invalidate()
                return true
            }
        }
        return super.onTouchEvent(ev)
    }
}
