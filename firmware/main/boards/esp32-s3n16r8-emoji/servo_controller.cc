/**
 * @file servo_controller.cc
 * @brief 舵机控制模块实现
 */

#include "servo_controller.h"
#include <esp_log.h>

#define TAG "ServoController"

ServoController::ServoController() {
}

ServoController::~ServoController() {
}

void ServoController::Initialize() {
    // 配置LEDC定时器
    ledc_timer_config_t ledc_timer = {
        .speed_mode = LEDC_MODE,
        .duty_resolution = LEDC_TIMER_BIT_WIDTH,
        .timer_num = LEDC_TIMER,
        .freq_hz = LEDC_FREQUENCY,
        .clk_cfg = LEDC_AUTO_CLK
    };
    ESP_ERROR_CHECK(ledc_timer_config(&ledc_timer));

    // 配置LEDC通道
    for (int i = 0; i < SERVO_CHANNEL_COUNT; i++) {
        ledc_channel_config_t ledc_channel = {
            .gpio_num = servo_pins[i],
            .speed_mode = LEDC_MODE,
            .channel = servo_channels[i],
            .intr_type = LEDC_INTR_DISABLE,
            .timer_sel = LEDC_TIMER,
            .duty = 0,
            .hpoint = 0
        };
        ESP_ERROR_CHECK(ledc_channel_config(&ledc_channel));
    }

    // 分步设置舵机初始位置 (防止两个舵机同时动作导致巨大的瞬时电流尖峰)
    ESP_LOGI(TAG, "正在初始化水平舵机...");
    SetServoAngle(0, SERVO_CENTER_X);
    vTaskDelay(pdMS_TO_TICKS(500)); // 等待第一个舵机稳定
    
    ESP_LOGI(TAG, "正在初始化垂直舵机...");
    SetServoAngle(1, SERVO_CENTER_Y);
}

void ServoController::SetServoAngle(int channel, int angle) {
    // 限制角度范围
    if (channel == 0) { // 水平舵机
        angle = std::max(SERVO_MIN_X, std::min(SERVO_MAX_X, angle));
        current_x_angle_ = angle;
    } else { // 垂直舵机
        angle = std::max(SERVO_MIN_Y, std::min(SERVO_MAX_Y, angle));
        current_y_angle_ = angle;
    }

    // 计算脉冲宽度 (考虑反转)
    uint32_t pulse_width;
    bool inverse = (channel == 0) ? (SERVO_X_INVERSE == 1) : (SERVO_Y_INVERSE == 1);
    
    if (inverse) {
        pulse_width = SERVO_MAX_PULSEWIDTH - (angle * (SERVO_MAX_PULSEWIDTH - SERVO_MIN_PULSEWIDTH)) / 180;
    } else {
        pulse_width = SERVO_MIN_PULSEWIDTH + (angle * (SERVO_MAX_PULSEWIDTH - SERVO_MIN_PULSEWIDTH)) / 180;
    }

    // 计算PWM占空比
    uint32_t duty = (pulse_width * ((1 << LEDC_TIMER_BIT_WIDTH) - 1)) / (1000000 / LEDC_FREQUENCY);

    // 设置并更新占空比
    ledc_set_duty(LEDC_MODE, servo_channels[channel], duty);
    ledc_update_duty(LEDC_MODE, servo_channels[channel]);
    
    // 开启日志，方便观察舵机是否收到了正确的数值
    // ESP_LOGI(TAG, "Channel:%d Angle:%d Inv:%d Pulse:%lu Duty:%lu", channel, angle, inverse, pulse_width, duty);
}

void ServoController::HeadMove(int x_offset, int y_offset, int servo_delay) {
    // 根据当前角度计算绝对目标
    int target_x = current_x_angle_ + x_offset;
    int target_y = current_y_angle_ + y_offset;
    
    // 调用绝对移动函数，它内部会处理边界保护和步进插值
    MoveTo(target_x, target_y, servo_delay);
}

void ServoController::MoveTo(int target_x, int target_y, int servo_delay) {
    // 强制限制目标角度在安全范围内 (边界保护)
    int clamped_x = std::max(SERVO_MIN_X, std::min(SERVO_MAX_X, target_x));
    int clamped_y = std::max(SERVO_MIN_Y, std::min(SERVO_MAX_Y, target_y));
    
    if (clamped_x == current_x_angle_ && clamped_y == current_y_angle_) {
        // 如果已在目标位置（或是已触碰边界无法再向该方向移动），则直接返回
        return;
    }

    // 平滑插值移动：逐步趋近目标位置
    while (current_x_angle_ != clamped_x || current_y_angle_ != clamped_y) {
        if (current_x_angle_ != clamped_x) {
            int diff = clamped_x - current_x_angle_;
            if (abs(diff) <= SERVO_STEP) {
                current_x_angle_ = clamped_x;
            } else {
                current_x_angle_ += (diff > 0 ? SERVO_STEP : -SERVO_STEP);
            }
            SetServoAngle(0, current_x_angle_);
        }
        if (current_y_angle_ != clamped_y) {
            int diff = clamped_y - current_y_angle_;
            if (abs(diff) <= SERVO_STEP) {
                current_y_angle_ = clamped_y;
            } else {
                current_y_angle_ += (diff > 0 ? SERVO_STEP : -SERVO_STEP);
            }
            SetServoAngle(1, current_y_angle_);
        }
        vTaskDelay(pdMS_TO_TICKS(servo_delay));
    }
}

void ServoController::HeadNod(int servo_delay) {
    // 确保最小延迟值，避免太快导致生硬
    int actual_delay = std::max(10, servo_delay);
    
    // 点头动作优化：使用 MoveTo 进行平滑移动，而不是直接 SetServoAngle
    for (int i = 0; i < 2; i++) {
        // 向下点头 (到最大垂角)
        MoveTo(current_x_angle_, SERVO_CENTER_Y + 15, actual_delay);
        // 向上回弹
        MoveTo(current_x_angle_, SERVO_CENTER_Y - 10, actual_delay);
    }
    
    // 恢复到中心位置
    MoveTo(current_x_angle_, SERVO_CENTER_Y, actual_delay);
}

void ServoController::HeadShake(int servo_delay) {
    // 确保最小延迟值
    int actual_delay = std::max(10, servo_delay);
    
    // 摇头动作优化：使用 MoveTo 实现，确保从当前位置平滑开始
    for (int i = 0; i < 2; i++) {
        // 向左摇头
        MoveTo(SERVO_CENTER_X - 25, current_y_angle_, actual_delay);
        // 向右摇头
        MoveTo(SERVO_CENTER_X + 25, current_y_angle_, actual_delay);
    }
    
    // 恢复到中心位置
    MoveTo(SERVO_CENTER_X, current_y_angle_, actual_delay);
}

void ServoController::HeadRoll(int servo_delay) {
    // 完全参考boardemoji.ino中的实现
    HeadCenter();
    HeadDown(SERVO_OFFSET_Y/2+5);
    HeadMove(SERVO_OFFSET_X, -SERVO_OFFSET_Y/2, servo_delay);
    HeadMove(-SERVO_OFFSET_X, -SERVO_OFFSET_Y/2, servo_delay);
    HeadMove(-SERVO_OFFSET_X, SERVO_OFFSET_Y/2, servo_delay);
    HeadMove(SERVO_OFFSET_X, SERVO_OFFSET_Y/2, servo_delay);
    HeadMove(-SERVO_OFFSET_X, -SERVO_OFFSET_Y/2, servo_delay);
    HeadMove(SERVO_OFFSET_X, -SERVO_OFFSET_Y/2, servo_delay);
    HeadMove(SERVO_OFFSET_X, SERVO_OFFSET_Y/2, servo_delay);
    HeadMove(-SERVO_OFFSET_X, SERVO_OFFSET_Y/2, servo_delay);
    HeadCenter();
}

void ServoController::HeadUp(int offset) {
    HeadMove(0, -offset);
}

void ServoController::HeadDown(int offset) {
    HeadMove(0, offset);
}

void ServoController::HeadLeft(int offset) {
    HeadMove(-offset, 0);
}

void ServoController::HeadRight(int offset) {
    HeadMove(offset, 0);
}

void ServoController::HeadCenter(int servo_delay) {
    // 参考原始boardemoji.ino中的实现，通过HeadMove方法逐步移动到中心位置
    // 计算当前位置到中心位置的偏移量
    int x_offset = SERVO_CENTER_X - current_x_angle_;
    int y_offset = SERVO_CENTER_Y - current_y_angle_;
    
    // 通过HeadMove方法逐步移动到中心位置
    HeadMove(x_offset, y_offset, servo_delay);
}
