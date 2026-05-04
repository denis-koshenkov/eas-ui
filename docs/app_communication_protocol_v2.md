# Application Communication Protocol v2

# Introduction

This page describes the **application** communication protocol that will be used between the EAS device and the computer.

# Types of messages

The EAS device needs to be able to send the following messages:

- Alert status change message - specifies in the payload if the alert is raised or silenced.

The computer sends the following messages to the EAS:

- Add alert message - Specifies all the alert details in the payload, as described in the “Structure of an Alert” page.
- Remove alert message - Specifies in the payload the alert id of alert to remove.

# Message Descriptions

## Alert status change message

- Byte 0: 0x0 - message ID
- Byte 1: alert ID
- Byte 2: 0x0 - alert is silenced, 0x1 - alert is raised

## Remove alert message

- Byte 0: 0x1 - message ID
- Byte 1: alert ID

## Add alert message

- Byte 0: 0x2 - message ID
- The following bytes represent all alert details: alert ID, warmup period, cooldown period, notification types, alert condition. The format is described in the `alert_structure.md` file.
