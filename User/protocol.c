/********************************** (C) COPYRIGHT *******************************
 * File Name          : protocol.c
 * Description        : UART command protocol parser (see protocol.h).
 *******************************************************************************/
#include "protocol.h"
#include "uart.h"
#include "buzzer.h"

/* Parser states. */
#define ST_HEAD1                    0U
#define ST_HEAD2                    1U
#define ST_MODE                     2U
#define ST_PAYLOAD                  3U
#define ST_CHECKSUM                 4U
#define ST_TAIL                     5U

static uint8_t s_state = ST_HEAD1;
static uint8_t s_mode = 0;
static uint8_t s_payload[PROTO_MAX_PAYLOAD_LEN];
static uint8_t s_index = 0;
static uint8_t s_expected = 0;
static uint8_t s_checksum = 0;
static uint16_t s_idle_ms = 0;

/* ------------------------------------------------------------------------- */
/* Helpers                                                                   */
/* ------------------------------------------------------------------------- */

/* Payload length of a mode, 0xFF when the mode is unknown. */
static uint8_t protocol_payload_len(uint8_t mode)
{
    switch(mode)
    {
        case PROTO_MODE_VERIFY:
            return 0U;
        case PROTO_MODE_LEDS:
            return PROTO_LED_PAYLOAD_LEN;
        case PROTO_MODE_BUZZER:
            return PROTO_BUZZER_PAYLOAD_LEN;
        default:
            return 0xFFU;
    }
}

/* Execute a completely received, checksum verified frame. */
static void protocol_dispatch(void)
{
    uint8_t i;

    switch(s_mode)
    {
        case PROTO_MODE_VERIFY:
            UART_SendString(PROTO_VERIFY_REPLY);
            break;

        case PROTO_MODE_LEDS:
            for(i = 0; i < WS2812_LED_NUM; i++)
            {
                WS2812_SetPixel(i,
                                s_payload[i * 3U + 0U],   /* R */
                                s_payload[i * 3U + 1U],   /* G */
                                s_payload[i * 3U + 2U]);  /* B */
            }
            WS2812_Refresh();
            break;

        case PROTO_MODE_BUZZER:
        {
            uint16_t freq_hz = (uint16_t)s_payload[0] | ((uint16_t)s_payload[1] << 8);
            uint8_t  volume = s_payload[2];
            uint16_t duration_ms = (uint16_t)s_payload[3] | ((uint16_t)s_payload[4] << 8);

            Buzzer_Beep(freq_hz, volume, duration_ms);
            break;
        }

        default:
            break;
    }
}

/* ------------------------------------------------------------------------- */
/* Public API                                                                */
/* ------------------------------------------------------------------------- */

void Protocol_Init(void)
{
    s_state = ST_HEAD1;
    s_mode = 0;
    s_index = 0;
    s_expected = 0;
    s_checksum = 0;
    s_idle_ms = 0;
}

void Protocol_Task(void)
{
    uint8_t byte;
    uint8_t received = 0;

    while(UART_ReadByte(&byte))
    {
        received = 1;

        switch(s_state)
        {
            case ST_HEAD1:
                if(byte == PROTO_HEAD1)
                {
                    s_checksum = byte;
                    s_state = ST_HEAD2;
                }
                break;

            case ST_HEAD2:
                if(byte == PROTO_HEAD2)
                {
                    s_checksum += byte;
                    s_state = ST_MODE;
                }
                else if(byte == PROTO_HEAD1)
                {
                    /* 55 55 -> the second byte is a new frame start */
                    s_checksum = byte;
                }
                else
                {
                    s_state = ST_HEAD1;
                }
                break;

            case ST_MODE:
                s_mode = byte;
                s_checksum += byte;
                s_expected = protocol_payload_len(byte);
                s_index = 0;

                if(s_expected == 0xFFU)
                {
                    s_state = ST_HEAD1;      /* unknown mode */
                }
                else if(s_expected == 0U)
                {
                    s_state = ST_CHECKSUM;
                }
                else
                {
                    s_state = ST_PAYLOAD;
                }
                break;

            case ST_PAYLOAD:
                s_payload[s_index] = byte;
                s_index++;
                s_checksum += byte;

                if(s_index >= s_expected)
                {
                    s_state = ST_CHECKSUM;
                }
                break;

            case ST_CHECKSUM:
                if(byte == s_checksum)
                {
                    s_state = ST_TAIL;
                }
                else
                {
                    s_state = ST_HEAD1;      /* checksum error, resync */
                }
                break;

            case ST_TAIL:
                if(byte == PROTO_TAIL)
                {
                    protocol_dispatch();
                }
                s_state = ST_HEAD1;
                break;

            default:
                s_state = ST_HEAD1;
                break;
        }
    }

    /* Watchdog for half received frames: if the sender stalls in the middle
     * of a frame, drop it instead of swallowing the next frames as payload. */
    if(received != 0U)
    {
        s_idle_ms = 0;
    }
    else if(s_state != ST_HEAD1)
    {
        if(s_idle_ms < PROTO_FRAME_TIMEOUT_MS)
        {
            s_idle_ms++;
        }

        if(s_idle_ms >= PROTO_FRAME_TIMEOUT_MS)
        {
            s_state = ST_HEAD1;
            s_idle_ms = 0;
        }
    }
    else
    {
        s_idle_ms = 0;
    }
}
