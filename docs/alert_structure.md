# Structure of an Alert

- One alert consists of:
    - Alert ID
    - Warmup period
    - Cooldown period
    - Notification type
    - An alert condition

# Alert ID

## General Description

- Used to uniquely identify an alert.
- Can only be a value between 0 and MAX_NUM_ALERTS-1.

## Payload

- 1 byte - `uint8_t` that holds the alert ID.

# Warmup Period

## General Description

Warmup period defines for how long the alert condition must evaluate to true until the alert is raised.

## Payload

The number of milliseconds as `uint32_t` - 4 bytes. Note that this is the time AFTER the last sample that makes that condition true is received. For example, assume that the sampling rate of the sensor producing the sample is 100 Hz. This means that there is at most 10 ms delay between the condition becoming true and the system detecting that the condition is true (plus some negligible computation time). Keep this in mind when defining the warmup period.

# Cooldown Period

## General Description

Cooldown period defines for how long the alert will stay raised after the alert condition became false.

The purpose of the cooldown period, as well as the warmup period, is to avoid the situation when a condition keeps toggling on and off, so the alarm is switching between raised and not raised.

## Payload

The number of milliseconds as `uint32_t` - 4 bytes. Same note applies to the cooldown period as to the warmup period.

# Notification Type

## General Description

The system can perform the following notifications:

- Send a message to the computer
- Set an LED to a certain color and pattern

The notification type specifies which, if any, of the notification types should be triggered when the alarm is raised.

## Payload

The first byte is a bitmap specifying which notification types should be triggered:

- Bit 0: send a message to the computer
- Bit 1: set an LED to a certain color and pattern

The first byte cannot be 0, because then no notifications will be triggered when the alert is raised.

One byte is enough for this, because the system will not have more than 8 notification types.

The first byte does not state which color and pattern the LED should have. Note that this applies only in the case when bit 1 is set.

If bit 1 is set in the first byte, then the next 2 bytes define the color and pattern of the LED.

- Second byte: color of the LED. Supported values:
    - 0x0 - red
    - 0x1 - green
    - 0x2 - blue
- Third byte: pattern of the LED. Supported values:
    - 0x0 - static
    - 0x1 - alert

# Alert Condition

## General Description

- A condition specifies when the alarm should be raised.
- A condition consists of one or more environment variable requirements separated by logical operators.
- On the top level, a condition is one or more ORed variable requirements separated by ANDs, i.e.:
    - Condition = <ORed req 1> AND <ORed req 2> AND <ORed req 3> …
    - It is also possible that:
    - Condition = <ORed req 1>
- What is an ORed requirement?
    - ORed requirement = <req 1> OR <req 2> OR <req 3> …
    - It is also possible that:
    - ORed requirement = <req 1>
- What is a variable requirement?
    - A variable requirement is a constraint on the value for the variable. For example, “temperature ≥ 20 degrees” is a variable requirement. A variable requirement consists of the following:
        - Variable identifier
        - Operator
        - Constraint value
    - Here is a definition for each of the three components of a variable requirement:
        - Variable identifier identifies what variable the requirement is for. Some examples of variable identifiers: temperature, humidity, ambient lighting, barometric pressure.
        - Operator defines how the constraint value is handled. Supported operators:
            - Greater than or equal to (≥)
            - Less than or equal to (≤)
        - Constraint value. For example, if operator is ≥ and value is “20”, then the variable requirement will evaluate to true when the value of the variable is equal to or greater than 20.

## Payload

How does an alert condition gets encoded when it is sent as a part of a message between device and computer?

Let us start with how a variable requirement gets encoded:

- Variable identifier: 1 byte. We will not support more than 256 environment variables.
- Operator: 1 byte. So far, only 2 operators are defined, but more might be added in the future (e.g. “equal to”).
    - Greater than or equal to (≥): value 0x0
    - Less than or equal to (≤): value 0x1
- Constraint value. The size of this is dynamic: it depends on the variable identifier. Each variable has its own units and supported ranges, so it does not make sense to define one size for all variables.
    - Temperature
        - Measured in Celsius with one decimal point precision.
        - Allowed range: -50.0 to 70.0 (both including) degrees Celsius.
        - Represented as a two-byte signed integer in little endian.
        - Examples:
            - -50.0 is -500
            - 70.0 is 700
            - 21.2 is 212
            - 0 is 0
    - Pressure
        - Measure in hPa with one decimal point precision.
        - Allowed range: 0 to 1500 (both including) hPa.
            - It is ~315 on top of mount Everest, and at sea level the maximal ever recorded is 1084.
        - Represented as a two-byte unsigned integer in little endian.
        - Examples:
            - 900 hPa -> 9000
            - 0 hPa -> 0
            - 1111 hPa -> 11110
            - 876.5 hPa -> 8765
            - 1500 hPa → 15000
    - Humidity
        - Humidity is represented as Relative Humidity (RH) in percentage with a precision of one decimal point.
        - Allowed range: 0 to 100 (both including) percent.
        - Represented as a two-byte unsigned integer in little endian.
        - Examples:
            - 0.0% RH → 0
            - 10.1% RH → 101
            - 100.0% RH → 1000
            - 95.5% RH → 955
    - Light Intensity
        - Light intensity is represented in whole lux (lx) with a precision of whole values (0 decimal points).
        - Allowed range: 0 to 130,000 (both including) lx.
        - Represented as a four-byte unsigned integer in little endian.
        - Examples:
            - 0 lx → 0
            - 555 lx → 555
            - 130,000 lx → 130000
            - 80456 lx → 80456

Thus, the size of a variable requirement payload is dynamic - it depends on the variable identifier. The parser reads the 1st byte - variable identifier - and it knows the total number of expected bytes in the encoding of a variable requirement.

Now, an alert condition can consist of several variable requirements.

The next unit to define is an ORed requirement. An ORed requirement is simply several variable requirements that are ORed together. An ORed requirement has the following payload:

- Number of variable requirements: 1 byte.
- Variable requirements: the number defined in the first byte.

Thus, we have one byte that defines the number of variable requirements, and then we have that number of variable requirements.

Since the whole condition is just a list of ORed requirements, the whole condition is defined by the same logic as an ORed requirement:

- Number of ORed requirements: 1 byte.
- ORed requirements: the number defined in the first byte.

Example: simple requirement that consists of 1 variable condition: temp ≥ 20.

The first byte is the number of ORed requirements. We will have just one in this case, since no conditions need to be ANDed with each other.

Then we have our one ORed requirement. The ORed requirement consists of only one variable requirement, so the second byte is also 1.

Next, we have the variable requirement itself:

- The third byte is the variable identifier or the variable “temp”
- The fourth byte is 0x0 - operator “greater than or equal”
- All bytes after the fifth one contain the value “20“. The number of bytes and units for the variable “temp” is yet to be defined. For example, we could have temperature defined as `uint16_t`, then we would have 2 bytes containing the value 20.
