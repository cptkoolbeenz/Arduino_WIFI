# Field Install Checklist

## Scope
This checklist covers power, PoE, grounding/surge protection, and commissioning for:
- 1 gateway/controller enclosure
- 3 Ubiquiti U6 Mesh AP runs
- 12V solar + battery power system

## 1. Pre-Install

### 1.1 Verify Parts
- Gateway + DC power lead
- Solar panel(s), MPPT charge controller, 12V battery
- Fused DC distribution block
- 12V-to-48V PoE switch/injector system (802.3af/at compatible)
- Ethernet surge protectors: 6 total (2 per AP run x 3 runs)
- DC surge protector for 12V bus
- Outdoor-rated Ethernet cable and weatherproof glands/connectors
- Ground bar, ground rod/system connection, bonding conductors, lugs
- Weatherproof metal enclosure
- Labels, heat-shrink, cable ties, strain relief

### 1.2 Pre-Label Cables
- `AP1_RUN`, `AP2_RUN`, `AP3_RUN`
- `GW_LAN`, `POE_UPLINK`
- `BAT_12V_POS`, `BAT_12V_NEG`
- `SOLAR_POS`, `SOLAR_NEG`

## 2. Power Wiring (Inside Enclosure)

### 2.1 Solar/Battery Backbone
- `Solar -> MPPT -> 12V Battery` (per MPPT manual)
- Battery positive to main fuse, then to fused distribution block
- Battery negative to negative bus bar

### 2.2 Surge on DC Bus
- Install DC surge protector on 12V bus
- Bond DC surge protector ground lug to enclosure ground bar

### 2.3 Gateway Power Branch
- Distribution block positive (2A fuse) to gateway `+`
- Negative bus to gateway `-`
- Verify polarity before power-up

### 2.4 PoE Power Branch
- Distribution block positive (PoE branch fuse) to PoE system input `+`
- Negative bus to PoE system input `-`
- Verify 12V input accepted by your PoE hardware

## 3. Ethernet/PoE Topology

### 3.1 Core Side
- Gateway LAN to switch/injector data uplink as designed
- For each AP run:
  - PoE output port -> core-side Ethernet surge protector -> field cable out

### 3.2 AP Side (Each AP)
- Field cable -> AP-side Ethernet surge protector -> short patch -> U6 Mesh
- Keep AP-side patch short and weather-protected

## 4. Grounding and Bonding

### 4.1 Ground System
- Install site ground rod or bond to existing code-compliant site ground
- Install ground bar inside enclosure

### 4.2 Bonding Points
Bond all of the following to the same ground bar/system:
- Enclosure chassis
- DC surge protector
- All core-side Ethernet surge protectors
- Mast/pole/metal mounting structures

### 4.3 Grounding Quality
- Keep ground conductors short and straight
- Avoid loops and unnecessary bends
- Use appropriate gauge and corrosion-resistant terminations

## 5. Cable and Mechanical

### 5.1 Data Runs
- Use outdoor-rated Ethernet (UV/wet-rated)
- AP runs up to 50m are acceptable
- Maintain bend radius and strain relief

### 5.2 Enclosure
- Use proper glands and drip loops
- Seal all unused ports
- Keep high-current and signal paths organized and separated

## 6. Commissioning

### 6.1 Electrical Checks
- Confirm battery voltage and polarity
- Confirm 12V bus present at distribution
- Confirm PoE system output present (48V side)

### 6.2 Gateway Checks
- Gateway boots cleanly
- Ethernet link up
- LAN IP assigned
- `bsm_network` process runs

### 6.3 AP Checks (AP1/AP2/AP3)
- AP powers by PoE
- Link establishes
- AP reachable on network

### 6.4 Ground/Surge Checks
- Continuity from each surge protector ground to ground bar
- Continuity from ground bar to site ground

### 6.5 Soak Test
- Run for 30-60 minutes
- Verify no random reboot or link flap
- Verify expected data transfer path

## 7. Maintenance Log (Each Site Visit)
- Date/time and technician
- Weather conditions
- Battery voltage (rest + under load)
- Gateway uptime/restarts
- AP uptime/restarts
- Signs of surge damage or water ingress
- Actions taken and parts replaced

## 8. Quick Troubleshooting
- No gateway boot: check branch fuse, polarity, battery voltage
- AP offline: check PoE output, both surge protectors, patch cable, connector seals
- Intermittent links: check grounding, cable terminations, water ingress, strain points
- Repeated failures after storms: inspect and replace sacrificial surge protectors

## 9. Sign-Off
- Installer name:
- Date/time completed:
- Site ID:
- Notes:
