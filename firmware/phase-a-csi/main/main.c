/*
 * Project Presence — Phase A CSI Firmware
 * ESP32-S3 Node: Raw CSI capture → UDP stream to Jetson
 *
 * What this does:
 *   - Connects to WiFi
 *   - Enables CSI capture on all received packets
 *   - Parses CSI frames (amplitude + phase per subcarrier)
 *   - Streams binary CSI frames to <keep-host> (Jetson) over UDP :5005
 *   - Blinks LED to confirm live operation
 *
 * Hardware: BAKODELOP ESP32-S3 N16R8 (DevKitC-1)
 * Build:    ESP-IDF v5.2+
 */

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"

/* ── Configuration ─────────────────────────────────────────────────────── */
/* Edit these to match your environment */
#define WIFI_SSID        CONFIG_CSI_WIFI_SSID       /* set in menuconfig */
#define WIFI_PASS        CONFIG_CSI_WIFI_PASSWORD    /* set in menuconfig */
#define TARGET_IP        CONFIG_CSI_TARGET_IP        /* Jetson IP: set in menuconfig */
#define TARGET_PORT      5005
#define NODE_ID          CONFIG_CSI_NODE_ID          /* 1-4, unique per node */
#define LED_GPIO         2                           /* onboard LED */

/* ── Frame format (matches RuView ADR-018 binary protocol) ─────────────── */
/*
 * Offset  Size  Field
 *   0       4   Magic: 0xC5110001
 *   4       1   Node ID
 *   5       1   Number of antennas
 *   6       2   Number of subcarriers (LE u16)
 *   8       4   Sequence number (LE u32)
 *  12       1   RSSI (i8)
 *  13       1   Noise floor (i8)
 *  14       2   Reserved
 *  16       N   Amplitude bytes (uint8, one per subcarrier per antenna)
 *  16+N     N   Phase bytes (uint8, scaled -128..127)
 *
 * Total header: 16 bytes
 * Total frame:  16 + (2 * n_antennas * n_subcarriers) bytes
 */
#define FRAME_MAGIC      0xC5110001
#define MAX_SUBCARRIERS  128
#define MAX_ANTENNAS     3
#define HEADER_SIZE      16
#define MAX_FRAME_SIZE   (HEADER_SIZE + 2 * MAX_ANTENNAS * MAX_SUBCARRIERS)

static const char *TAG = "presence-csi";

/* ── Globals ────────────────────────────────────────────────────────────── */
static int udp_sock = -1;
static struct sockaddr_in dest_addr;
static uint32_t seq_num = 0;
static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

/* ── WiFi event handler ─────────────────────────────────────────────────── */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected, reconnecting...");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/* ── WiFi init ──────────────────────────────────────────────────────────── */
static void wifi_init(void)
{
    wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_connect());

    /* Wait for connection */
    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "WiFi connected");
}

/* ── UDP socket init ────────────────────────────────────────────────────── */
static void udp_init(void)
{
    udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (udp_sock < 0) {
        ESP_LOGE(TAG, "Failed to create UDP socket: errno %d", errno);
        return;
    }

    memset(&dest_addr, 0, sizeof(dest_addr));
    dest_addr.sin_family      = AF_INET;
    dest_addr.sin_port        = htons(TARGET_PORT);
    dest_addr.sin_addr.s_addr = inet_addr(TARGET_IP);

    ESP_LOGI(TAG, "UDP socket ready → %s:%d", TARGET_IP, TARGET_PORT);
}

/* ── CSI callback — fires for every received WiFi packet ────────────────── */
static void IRAM_ATTR csi_callback(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf || udp_sock < 0) return;

    wifi_pkt_rx_ctrl_t *ctrl = &info->rx_ctrl;
    int n_sub  = info->len / 2;          /* complex I/Q pairs → subcarrier count */
    int n_ant  = 1;                       /* ESP32-S3 DevKitC has 1 antenna */

    /* Clamp to buffer limits */
    if (n_sub > MAX_SUBCARRIERS) n_sub = MAX_SUBCARRIERS;

    /* Build frame buffer */
    uint8_t frame[MAX_FRAME_SIZE];
    int     offset = 0;

    /* Magic */
    uint32_t magic = FRAME_MAGIC;
    memcpy(frame + offset, &magic, 4);   offset += 4;

    /* Node ID */
    frame[offset++] = (uint8_t)NODE_ID;

    /* Antenna count */
    frame[offset++] = (uint8_t)n_ant;

    /* Subcarrier count (LE u16) */
    uint16_t n_sub16 = (uint16_t)n_sub;
    memcpy(frame + offset, &n_sub16, 2); offset += 2;

    /* Sequence number (LE u32) */
    memcpy(frame + offset, &seq_num, 4); offset += 4;
    seq_num++;

    /* RSSI and noise floor */
    frame[offset++] = (uint8_t)(int8_t)ctrl->rssi;
    frame[offset++] = (uint8_t)(int8_t)ctrl->noise_floor;

    /* Reserved */
    frame[offset++] = 0;
    frame[offset++] = 0;

    /*
     * Raw CSI buffer layout from ESP-IDF:
     * Pairs of int8_t [imaginary, real] per subcarrier.
     * We compute amplitude = sqrt(I^2 + Q^2) scaled to uint8.
     * We compute phase = atan2(I, Q) scaled to int8.
     */
    int8_t  *csi_raw = (int8_t *)info->buf;

    /* Amplitude bytes */
    for (int i = 0; i < n_sub; i++) {
        int8_t  imag = csi_raw[2 * i];
        int8_t  real = csi_raw[2 * i + 1];
        float   amp  = sqrtf((float)(real * real) + (float)(imag * imag));
        /* Scale: typical max amplitude ~60, map to 0-255 */
        uint8_t amp_u8 = (uint8_t)(amp * 4.0f > 255.0f ? 255 : amp * 4.0f);
        frame[offset++] = amp_u8;
    }

    /* Phase bytes */
    for (int i = 0; i < n_sub; i++) {
        int8_t imag   = csi_raw[2 * i];
        int8_t real   = csi_raw[2 * i + 1];
        float  phase  = atan2f((float)imag, (float)real); /* -π to +π */
        /* Scale to int8: -π→-128, +π→+127 */
        int8_t phase_i8 = (int8_t)(phase * 40.0f);
        frame[offset++] = (uint8_t)phase_i8;
    }

    /* Send UDP frame — non-blocking, drop if socket busy */
    sendto(udp_sock, frame, offset, MSG_DONTWAIT,
           (struct sockaddr *)&dest_addr, sizeof(dest_addr));
}

/* ── Enable CSI on WiFi interface ───────────────────────────────────────── */
static void csi_init(void)
{
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,   /* Legacy Long Training Field */
        .htltf_en          = true,   /* HT Long Training Field */
        .stbc_htltf2_en    = true,   /* STBC HT LTF */
        .ltf_merge_en      = true,   /* Merge LTFs for better SNR */
        .channel_filter_en = false,  /* Raw, unfiltered — we filter on Jetson */
        .manu_scale        = false,
    };

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&csi_callback, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "CSI capture enabled — streaming to %s:%d", TARGET_IP, TARGET_PORT);
}

/* ── LED blink task — visual heartbeat ──────────────────────────────────── */
static void led_task(void *arg)
{
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);

    while (1) {
        gpio_set_level(LED_GPIO, 1);
        vTaskDelay(pdMS_TO_TICKS(50));
        gpio_set_level(LED_GPIO, 0);
        vTaskDelay(pdMS_TO_TICKS(950));
    }
}

/* ── Stats task — logs frame rate every 10s ─────────────────────────────── */
static void stats_task(void *arg)
{
    uint32_t last_seq = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        uint32_t delta = seq_num - last_seq;
        last_seq = seq_num;
        ESP_LOGI(TAG, "Node %d — frames sent: %lu total, ~%lu Hz",
                 NODE_ID, (unsigned long)seq_num, (unsigned long)(delta / 10));
    }
}

/* ── Entry point ────────────────────────────────────────────────────────── */
void app_main(void)
{
    ESP_LOGI(TAG, "Project Presence — Phase A CSI Node %d starting", NODE_ID);

    /* NVS required for WiFi */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    wifi_init();
    udp_init();
    csi_init();

    xTaskCreate(led_task,   "led",   1024, NULL, 1, NULL);
    xTaskCreate(stats_task, "stats", 2048, NULL, 1, NULL);

    ESP_LOGI(TAG, "CSI pipeline running. Streaming to %s:%d", TARGET_IP, TARGET_PORT);
}
