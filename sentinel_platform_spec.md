**SENTINEL**

Ambient Intelligence Platform

**Product Vision & Technical Specification**

Version 0.3 \| March 14, 2026 \| CONFIDENTIAL

# 1. Executive Summary

Sentinel is a passive, multi-modal ambient intelligence platform that
knows who or what is present in any space, continuously and without
interaction. It combines radar, thermal imaging, LiDAR, optical,
acoustic, and chemical sensing into a unified fusion engine that
produces a living world model of every zone it monitors.

> *Nobody is building local, private, multi-modal presence intelligence
> at consumer price points. Sentinel fills that gap.*

The platform has three distinct go-to-market applications on a single
hardware and software stack:

  -----------------------------------------------------------------------
  **PRODUCT**     **VALUE PROPOSITION**       **PRIMARY MARKET**
  --------------- --------------------------- ---------------------------
  **Sentinel      Identity-aware security --- Residential, commercial,
  Secure**        know who is present,        healthcare, critical
                  whether they belong, and    infrastructure
                  whether they are alive      

  **Sentinel      Identity-aware automation   High-end residential,
  Living**        --- rooms respond to who is hospitality, senior living
                  present and their current   
                  physiological state         

  **Sentinel      Passive wellness monitoring Healthcare, senior living,
  Health**        --- continuous vitals       wellness facilities
                  baseline, anomaly           
                  detection, no wearable      
                  required                    
  -----------------------------------------------------------------------

# 2. The Problem

## 2.1 Current Security Systems

Traditional alarm systems rely on binary contact sensors and passive
infrared motion detection. They answer one question: did something
happen? They cannot answer: who is there, are they authorized, are they
alive, and is their behavior normal?

Modern connected systems (Ring, SimpliSafe, ADT Command, Alarm.com) add
cloud AI for basic optical classification but remain fundamentally
reactive. They detect events after the fact and route alerts to central
monitoring stations using decades-old transmission protocols (Contact
ID, SIA DC-09).

Enterprise systems (Verkada, Avigilon, Milestone) add AI analytics to
optical cameras but remain cloud-dependent, subscription-based, and
optically limited. They cannot see in darkness, through occlusion, or
detect presence without line of sight.

## 2.2 Current Home Automation

Smart home presence detection is binary --- something is here or it is
not. No commercial system knows who is present, what they are doing, or
what their physiological state is. Scenes and automations are manually
configured rules, not adaptive responses to identity and state.

## 2.3 The Gap

  ---------------------------------------------------------------------------------
  **CAPABILITY**        **Traditional   **Modern      **Enterprise   **SENTINEL**
                        Alarm**         Connected**   Camera**       
  --------------------- --------------- ------------- -------------- --------------
  Room-scale presence   ✓               ✓             ✓              **✓**
  detection                                                          

  Works in darkness /   Partial         ✗             ✗              **✓**
  occlusion                                                          

  Person vs animal      ✗               Partial       ✓              **✓**
  classification                                                     

  Passive identity (no  ✗               ✗             Partial        **✓**
  badge/code)                                                        

  Liveness proof (not a ✗               ✗             ✗              **✓**
  photo/dummy)                                                       

  Behavioral baseline + ✗               ✗             Partial        **✓**
  anomaly                                                            

  Physiological state   ✗               ✗             ✗              **✓**
  awareness                                                          

  Fully local / no      ✓               ✗             ✗              **✓**
  cloud required                                                     

  No subscription fee   ✓               ✗             ✗              **✓**
  ---------------------------------------------------------------------------------

# 3. System Architecture

## 3.1 Architecture Overview

Sentinel is a two-component system: a network of Sentinel Nodes (sensor
hardware) and a Sentinel Hub (edge compute and intelligence). Nodes are
passive data collectors. The Hub runs the fusion engine, identity
engine, behavior engine, and agent layer.

> *The IP and defensible moat is in the fusion layer --- not the
> hardware. Hardware gets copied. A trained multi-modal fusion model
> refined across real deployments does not.*

## 3.2 Processing Pipeline

Data flows through five sequential layers:

  ----------------------------------------------------------------------------------
  **LAYER**          **ENGINE**   **FUNCTION**
  ------------------ ------------ --------------------------------------------------
  **1 ---            **Sensor     Something is here --- radar, thermal, LiDAR,
  Detection**        Fusion**     acoustic triangulated

  **2 ---            **Fusion     What is it --- person, child, pet, object,
  Classification**   Engine**     environmental artifact

  **3 ---            **Identity   Who is it --- thermal vascular map, gait
  Identification**   Engine**     signature, rPPG profile, VOC baseline

  **4 ---            **Behavior   Do they belong --- profile match, time/zone rules,
  Authorization**    Engine**     behavioral baseline

  **5 --- Action**   **Sentinel   What to do --- LLM or rule engine reasons over
                     Agent**      world model, executes via MCP
  ----------------------------------------------------------------------------------

## 3.3 World Model Schema

The Hub maintains a continuously updated world model published to MQTT:

{ \"zones\": { \"office\": { \"occupants\": \[{ \"id\": \"<user>\",
\"confidence\": 0.94, \"authorized\": true, \"liveness\": true,
\"state\": \"seated_focused\", \"anomaly\": false, \"vitals\": {
\"breathing_rate\": 14, \"stress_index\": 0.3 } }\], \"unknown_count\":
0 } }, \"timestamp\": \"2026-03-11T09:22:00Z\" }

## 3.4 Distributed Holographic Memory Architecture

The Hub (Brain) is not a separate tier above nodes --- it is a peer
node with a coordination role. Role is configuration, not hierarchy.
Any node can be promoted to Brain. Memory is distributed holographically
across all nodes --- like a RAID array where each piece contains a
representation of the whole. No single node failure loses any knowledge.

### Memory Layers Per Node

Each node maintains three memory layers:

  --------------------------------------------------------------------------
  **LAYER**              **SCOPE**          **STORAGE**     **RESOLUTION**
  ---------------------- ------------------ --------------- -----------------
  **Local Experience**   Own zone only      SQLite          Full resolution
                                            on-node

  **System Digest**      All zones,         SQLite,         Compressed
                         compressed         synced from
                                            peers

  **Reasoning Log**      Own conclusions    Append-only     Full resolution
                                            log on-node
  --------------------------------------------------------------------------

The Brain holds full resolution across all zones. Sensor nodes hold full
resolution locally plus a compressed digest of the whole system. Any node
can reconstruct a coarse world picture from its digest alone.

### Lateral Memory Passing

Adjacent nodes share observations directly via MQTT peer topics without
waiting for Brain. An entry node primes the office node before a person
arrives. Brain synthesizes; it does not gatekeep.

MQTT lateral pattern: `home/node/{id}/peer/{neighbor_id}`

### Resilience Properties

-   **Brain failure** --- nodes continue autonomously, sync on reconnect
-   **Node failure** --- peers retain compressed representation of that zone
-   **Network partition** --- isolated nodes operate autonomously,
    reconcile on reconnect
-   **New node joining** --- bootstraps from any peer digest, no central
    enrollment required

## 3.5 Network Security Architecture

Two distinct security surfaces with different threat models:

  --------------------------------------------------------------------------
  **SURFACE**            **SECURITY MODEL**
  ---------------------- ---------------------------------------------------
  **Human ↔ Brain**      Authenticated, encrypted, accessible off-network

  **Node ↔ Node /        Mutually authenticated, encrypted, no rogue nodes
  Node ↔ Brain**
  --------------------------------------------------------------------------

### Inter-Node Security

mTLS on MQTT. Every node holds a certificate issued by a local CA on
<keep-host>. Mosquitto enforces mTLS --- no node connects without a valid
cert. Rogue node prevention is built in. Lateral peer channels carry the
same mTLS guarantee.

### Node Enrollment

Physical access to <keep-host> is required to issue a certificate. This is
the moment of trust establishment. No remote enrollment.

### Human ↔ Brain Channels

-   **Local HTTPS API** --- token authenticated, serves dashboard and queries
-   **Telegram bot** --- end-to-end encrypted, works off-network, already
    in stack via n8n
-   **MCP server** --- authenticated agent tool invocation

### Data Protection

-   World model digests encrypted before transmission and at rest
-   Biometric profiles encrypted at rest on <keep-host>
-   Reasoning logs append-only and integrity-protected

### Local CA on <keep-host>

step-ca or cfssl. Issues, rotates, and revokes node certificates. Root
of trust for the entire network.

### Sections Anticipated to Grow

Key rotation policy, node decommission workflow, off-network access
options, audit log tamper evidence, future TPM integration on nodes.

# 4. Sensor Stack

## 4.1 Sensor Layer Overview

Each sensor layer contributes distinct detection capabilities. No single
layer is sufficient. The fusion of all layers produces a result that is
extremely difficult to spoof simultaneously.

  -----------------------------------------------------------------------------------------------------------------------------
  **LAYER**      **Detect**   **Classify**   **Identify**   **Liveness**   **Anomaly**   **Through-wall**   **Dark/Occluded**
  -------------- ------------ -------------- -------------- -------------- ------------- ------------------ -------------------
  **Radar**      ✓✓           ✓✓             ✗              ✓              ✓             ✓✓                 ✓✓

  **Thermal**    ✓✓           ✓              ✓✓             ✓✓             ✓✓            ✗                  ✓✓

  **LiDAR**      ✓✓           ✓✓             ✓              ✗              ✓             ✗                  ✓

  **Optical**    ✓            ✓✓             ✓✓             ✓✓             ✓             ✗                  ✗

  **Acoustic**   ✓            ✓              ✓ partial      ✗              ✓             ✗                  ✓

  **Chemical**   ✗            ✗              ✗              ✓              ✓✓            ✗                  ✓✓

  **BLE/WiFi     ✓            ✗              ✓✓             ✗              ✓             ✓✓                 ✓✓
  Device Scan**
  -----------------------------------------------------------------------------------------------------------------------------

> *BLE/WiFi passive scanning is a zero-cost 7th sensor layer. Every
> ESP32-S3 node already has both radios. It detects known vs unknown
> devices, correlates devices to identities, and flags the absence of a
> device paired with physical presence as a threat signal.*

## 4.2 Prototype Hardware BOM

Target cost: sub-\$250 per full-stack node using commodity hardware. The
Jetson Hub is shared across all nodes.

  -------------------------------------------------------------------------------------
  **LAYER**      **COMPONENT**    **PART**         **COST**      **NOTES**
  -------------- ---------------- ---------------- ------------- ----------------------
  **Radar**      24GHz Doppler    **HLK-LD2450**   **\$15**      Multi-target tracking,
                 Presence                                        3 zones, UART ---
                                                                 already HA-integrated

  **Thermal**    Thermal USB      **Topdon          **\$199**     256x192 native, 512x384
                 Camera           TC001**                         TISR upscale, USB-C UVC
                                                                 device --- Linux/Pi
                                                                 compatible, confirmed

  **LiDAR**      2D 360° Scanner  **YDLIDAR         **\$80**      10m range, upgraded
                                  X4PRO**                         optics, room geometry
                                                                 + perimeter mapping

  **Optical**    RGB + IR Camera  **RPi Cam v2 +   **\$25**      rPPG pulse extraction,
                                  IR**                           gait, face --- Jetson
                                                                 runs inference

  **Acoustic**   MEMS Mic Array   **SPH0645**      **\$20**      Directional presence,
                 x4                                              footstep, voice
                                                                 presence

  **Chemical**   VOC /            **BME688**       **\$20**      VOC, temp, humidity,
                 Environmental                                   pressure --- metabolic
                                                                 anomaly baseline

  **BLE/WiFi**   Passive Device   **ESP32-S3        **\$0**       Known/unknown device
                 Scanner          (built-in)**                   registry, device-to-
                                                                 identity correlation,
                                                                 zero additional hardware

  **Compute**    Edge MCU per     **ESP32-S3**     **\$5**       Sensor aggregation,
                 node                                            UDP to Jetson --- same
                                                                 as CSI nodes

  **Hub**        Edge AI Compute  **Jetson Orin    **\$0**       Already owned ---
                                  Nano**                         <keep-host>

  **TOTAL**      Full stack                        **\~\$344**   7 sensor layers +
                 prototype node                                  compute, commodity
                                                                 hardware
  -------------------------------------------------------------------------------------

## 4.3 Spoof Resistance Analysis

Each physical attack vector is defeated by a specific combination of
sensor layers:

  -----------------------------------------------------------------------
  **ATTACK VECTOR**  **DEFEATED BY**
  ------------------ ----------------------------------------------------
  **Photo or         Thermal --- no heat gradient; radar --- no
  screen**           micro-motion

  **Mannequin /      Radar micro-motion + VOC --- no organic chemical
  dummy**            signature

  **Mask / facial    Thermal vascular map is subcutaneous --- cannot be
  prosthetic**       faked externally

  **Replay video     Radar + acoustic do not match video timing
  attack**           

  **Impersonation    Gait + thermal map + VOC metabolic profile combined
  (live person)**    --- all three must match

  **Unknown          No enrolled profile match across any layer --- flags
  authorized         and monitors
  person**           

  **Animal (pet)**   LiDAR body volume + radar micro-motion signature ---
                     distinct from human

  **Environmental    Multi-layer confirmation required --- HVAC, curtain,
  artifact**         shadow all fail at least 3 layers

  **Unknown device   BLE/WiFi scan detects device with no matching physical
  (planted           presence on radar/thermal --- flagged as anomaly
  device)**

  **Phone-less       Physical presence confirmed by radar/thermal but NO
  intruder**         device detected --- absence of expected signal raises
                     threat score
  -----------------------------------------------------------------------

## 4.4 Barometric Fingerprinting

Multi-node BME688 deployment enables pressure differential analysis as a
passive event detection layer:

-   Door/window open events --- detected via rapid pressure equalization
    between zones with different baseline pressures

-   Floor detection --- barometric pressure difference between floors is
    ~0.3-0.4 hPa, sufficient to determine which floor an occupant is on
    when correlated with radar tracking

-   HVAC state inference --- pressure signature changes when HVAC cycles
    on/off, providing environmental context without smart thermostat
    integration

This requires no additional hardware --- the BME688 already ordered for
VOC sensing includes a barometric pressure sensor. The intelligence is
purely in the fusion layer's cross-node pressure correlation.

# 5. Node Deployment Strategy

## 5.1 Tiered Node Architecture

Not every zone requires the full sensor stack. Identity only needs to be
confirmed at transition points --- choke points where people move
between zones. Once identified, the system tracks occupants room-to-room
via inexpensive radar nodes.

> *This is how airport security works --- thorough check at entry,
> lighter touch through the terminal. Identity is carried as a token,
> not re-verified at every point.*

  --------------------------------------------------------------------------------
  **TIER**     **PLACEMENT**    **SENSORS**      **COST**      **FUNCTION**
  ------------ ---------------- ---------------- ------------- -------------------
  **Tier 1     Front door,      Full stack ---   **\~\$344**   Identity
  Entry /      garage entry,    all 7 sensor                   enrollment +
  Identity     rear door, gate  layers incl.                   confirmation,
  Gate**                        BLE/WiFi scan                  liveness proof,
                                                               full biometric
                                                               capture

  **Tier 2     Living room,     Radar + 2D LiDAR **\~\$95**    Occupancy tracking,
  Room Node**  kitchen, office, + BLE/WiFi scan                identity handoff
               bedroom, hallway                                from entry node,
                                                               device correlation

  **Tier 3     Exterior, yard,  Radar + BLE/WiFi **\~\$20**    Early approach
  Perimeter    driveway,        scan                           detection,
  Node**       parking          (weatherproof)                 perimeter breach,
                                                               device detection
  --------------------------------------------------------------------------------

## 5.2 Whole Home Budget

  -----------------------------------------------------------------------------
  **NODE TYPE**          **QTY         **UNIT    **SUBTOTAL**   **NOTES**
                         (typical)**   COST**                   
  ---------------------- ------------- --------- -------------- ---------------
  Tier 1 Entry nodes     3             \$344     **\$1,032**    Front, garage,
  (full stack)                                                  rear

  Tier 2 Room nodes      8             \$95      **\$760**      Main living
  (radar+LiDAR+BLE)                                             zones

  Tier 3 Perimeter nodes 6             \$20      **\$120**      Exterior
  (radar+BLE)                                                   coverage

  Sentinel Hub (Jetson)  1             \$0       **\$0**        Already owned

  **TOTAL --- WHOLE HOME 17 nodes                **\$1,912**    Hardware only,
  COVERAGE**                                                    prototype BOM
  -----------------------------------------------------------------------------

# 6. Software Architecture

## 6.1 Component Overview

All processing runs locally on the Sentinel Hub (Jetson). No cloud
dependency. No subscription required.

  ------------------------------------------------------------------------------
  **COMPONENT**    **FILE**                 **FUNCTION**
  ---------------- ------------------------ ------------------------------------
  **CSI Bridge**   csi_bridge.py            Existing --- WiFi CSI presence
                                            detection, MQTT publisher (built,
                                            running)

  **Sensor         sentinel_fusion.py       Aggregates all node sensor streams,
  Fusion**                                  computes confidence scores per zone

  **Identity       identity_engine.py       Thermal + gait + rPPG + VOC profile
  Engine**                                  matching, enrollment workflow

  **Behavior       behavior_engine.py       Baseline learning, temporal anomaly
  Engine**                                  detection, routine modeling

  **Sentinel MCP** sentinel_mcp_server.py   Exposes world model and controls as
                                            MCP tools --- agent integration
                                            point

  **Sentinel       sentinel_agent.py        LLM or rule engine --- reasons over
  Agent**                                   world model, executes actions

  **Node           sentinel_node.ino        ESP32-S3 --- sensor aggregation, UDP
  Firmware**                                to Hub, same pattern as CSI nodes

  **Digest Sync    digest_sync.py           Compressed world model sync between
  Service**                                 peers --- holographic memory
                                            replication

  **Peer MQTT      peer_channel.py          Lateral node-to-node communication
  Handler**                                 handler --- MQTT peer topic routing

  **Local CA       ca_service.py            Node certificate issuance,
  Service**                                 rotation, and revocation on <keep-host>
                                            (step-ca or cfssl wrapper)

  **Fast Path      fast_path.py             Lightweight independent alert
  Service**                                 trigger --- unknown presence →
                                            alert, no agent dependency

  **Degradation    degradation_fsm.py       Explicit operating mode state
  State Machine**                           machine --- Full / Degraded /
                                            Minimal transitions

  **Meta-          meta_reasoner/           Four-component autonomous reasoning
  Reasoner**       service.py               layer --- Curiosity / Desire / Drive
                                            / Action --- watches the agent and
                                            evaluates reasoning quality

  **AV             av_principles.py         Six AV-derived design patterns as
  Principles**                              code --- trust decay, shadow mode,
                                            fast/slow split, degradation FSM,
                                            long tail logging, calibration
                                            scenarios --- operating framework
                                            for Meta-Reasoner

  **Calibration    csi_bridge.py            SQLite persistence of calibration
  Store**          (CalibrationStore)       baselines --- survives restarts,
                                            loads fresh baselines on startup,
                                            24-hour freshness window

  **Brain /        brain/service.py         World model maintenance ---
  Narrative**      brain/narrative.py       NarrativeEngine tracking zone
                                            occupancy, actor states,
                                            home-level summary

  **CSI            adapters/                Legacy topic translation ---
  Adapter**        csi_adapter.py           home/csi/* to sentinel/sensors/*
                                            with confidence certificates
  ------------------------------------------------------------------------------

## 6.2 MCP Server Tool Schema

The Sentinel MCP server exposes the world model and actuator controls as
tools that any LLM agent can call:

  -----------------------------------------------------------------------------------
  **TOOL**                        **CATEGORY**   **RETURNS / EFFECT**
  ------------------------------- -------------- ------------------------------------
  get_zone_occupancy(zone)        **Read**       Occupant list with identity
                                                 confidence, authorization, liveness
                                                 per zone

  get_identity_confidence(zone)   **Read**       Best match profile + confidence
                                                 score + which sensors contributed

  get_behavioral_anomaly(zone)    **Read**       Anomaly flag, deviation type,
                                                 severity score, recommended action

  get_physiological_state(zone)   **Read**       Breathing rate, stress index,
                                                 activity level per confirmed
                                                 occupant

  get_world_model()               **Read**       Full current world model JSON across
                                                 all zones

  get_room_context(zone)          **Read**       Fused context: who, how many,
                                                 activity, state, routine match

  trigger_alert(zone, level, msg) **Actuate**    Push notification, siren, camera
                                                 record --- level: info/warn/critical

  set_room_scene(zone, scene)     **Actuate**    Set lighting/HVAC/audio scene via
                                                 Home Assistant

  lock_zone(zone)                 **Actuate**    Lock doors in zone via smart lock
                                                 integration

  enroll_occupant(name, zone)     **Admin**      Start 60-sec enrollment capture ---
                                                 builds biometric profile

  learn_preference(occupant, ctx) **Admin**      Record automation preference for
                                                 context --- feeds behavior engine

  get_known_devices(zone)          **Read**       List of known BLE/WiFi devices
                                                 currently detected in zone, with
                                                 identity correlation

  get_unknown_devices()            **Read**       All unrecognized BLE/WiFi devices
                                                 across all zones --- MAC, signal
                                                 strength, first/last seen, nearest
                                                 node

  enroll_device(mac, occupant)     **Admin**      Associate a BLE/WiFi MAC address
                                                 with an enrolled occupant profile

  log_event(type, zone, data)     **Admin**      Write event to audit log with full
                                                 sensor state snapshot
  -----------------------------------------------------------------------------------

## 6.3 Agent Tiers

The agent layer is swappable --- same MCP server, different reasoning
engine. This creates a natural product tier model:

  ------------------------------------------------------------------------
  **TIER**      **ENGINE**      **CAPABILITY**    **USE CASE**
  ------------- --------------- ----------------- ------------------------
  **Sentinel    Rule engine     Deterministic,    Cost-sensitive installs,
  Basic**                       fast, predictable simple security

  **Sentinel    Local LLM       Reasoning, fully  Privacy-first
  Pro**         (Ollama)        private, no cloud residential, healthcare

  **Sentinel    Claude / GPT-4o Maximum           High-value commercial,
  Elite**                       intelligence,     enterprise
                                explainable       
  ------------------------------------------------------------------------

## 6.4 Intelligence & Memory Architecture

### Design Philosophy

Sentinel does not learn a home through statistical pattern matching. It
builds a **world model** --- a living, causal understanding of the
physical environment and the humans within it. The distinction matters:
pattern matching tells you "this usually happens at 9am." A world model
tells you *why* it happens, *what it means*, and *what should happen
next*.

The intelligence architecture rests on three pillars:

**Context** --- the world model. The complete interpreted state of the
home at any moment. Not raw sensor data, but understood reality.
"Alice is cooking dinner" is context. "Thermal blob in zone 4 with
elevated motion" is just data. Context is the answer to: *what is true
right now?*

**Intent** --- the reasoning engine. Causal inference about *why*
something is happening, derived from context plus narrative history.
If a person walks to the front door at 8am on a weekday, intent is
inferred: they are leaving for work. Intent enables anticipation ---
the system acts on what is about to happen, not just what already did.

**Specification** --- the user contract. What the system should care
about and how it should respond, defined per household. Specification
is configuration, not code. A family with children has different specs
than an elderly person living alone. The reasoning engine is universal;
the specification makes each installation unique.

This separation is what makes Sentinel scalable. Context and intent are
built once as a general engine. Specification is what adapts it to any
home, any user, any use case.

### World Model: Two Layers

#### 6.4.1 The Physical Model --- "What is the world?"

A spatial-relational map that the system builds and maintains. Not just
room labels and zones, but understood physical relationships:

-   The desk is near a window (affects thermal readings at certain hours)
-   The hallway connects the bedroom to the kitchen (constrains movement paths)
-   ESP32 node positions relative to furniture and walls (determines CSI
    propagation and shadow patterns)
-   A door being open changes airflow, WiFi propagation, and acoustic
    characteristics simultaneously

The physical model means the system can reason about sensor changes
rather than just flag them. When furniture moves, the thermal signature
in a zone changes --- but so does WiFi propagation. The system
correlates the two and concludes "the environment changed" rather than
"anomaly detected." A new heat source that also shifts CSI patterns is
a space heater, not an intruder.

**Cold start:** The physical model is seeded during installation.
Room layout, sensor positions, major furniture. The system refines it
continuously but can reason from first principles on Day 1.

#### 6.4.2 The Narrative Model --- "What is happening and why?"

Instead of storing events as timestamped database rows, the system
maintains a **running causal narrative** --- a story about the household
where each event has context from what came before.

*"Alice came home at 6pm (door sensor + face rec + WiFi CSI). He
went to the kitchen (thermal tracking + radar). He has been there 20
minutes with elevated movement (cooking). Now he has moved to the living
room with reduced movement (eating or resting)."*

The narrative is not a log. It is a **causal chain** where each new
event is evaluated against the story so far:

-   Heart rate elevated + just exercised = expected, no alert
-   Heart rate elevated + sitting still for an hour = flag for attention
-   Unknown thermal signature in hallway + Alice just opened front
    door + second WiFi body with no enrolled bio-sig = likely guest,
    wait for face rec before alarming

This is what transforms a sensor grid into situational awareness. The
same sensor reading means completely different things depending on the
narrative that precedes it.

### Memory Architecture

Memory serves the world model, not the other way around. Four layers,
each feeding the narrative and physical models:

**Working Memory** (seconds--minutes) --- The current world state.
Rolling sensor fusion output published continuously via MQTT. "Alice
is at his desk, heart rate 68, been there 45 minutes." This is the
real-time narrative. Implementation: in-memory on Pi/Jetson, Redis if
distributed.

**Episodic Memory** (hours--days) --- What happened. Event log with
full sensor state snapshots at each significant transition. Every
meaningful event captures the complete context so the system can replay
and analyze. Feeds the narrative model with recent history.

**Pattern Memory** (weeks--months) --- What is normal. Learned routines
and baselines per person, per zone, per time window. Standard deviation
models that define the envelope of expected behavior. After two weeks,
the system knows what "normal Tuesday evening" looks like for each
person. Feeds the intent engine --- routine patterns are how the system
infers what you are *about to do*.

**Identity Memory** (permanent) --- Who you are. Enrolled biometric
profiles, learned sensor reliability scores per person per room, and
composite identity confidence thresholds. "WiFi CSI is 92% reliable for
identifying Alice at his desk but only 60% in the kitchen." Feeds the
physical model with per-person, per-zone calibration data.

### The Reasoning Loop

The agent does not query a database and apply rules. It maintains a
mental model and updates a story:

    sensor input
        → physical model update (did the environment change?)
        → narrative update (what does this mean in context?)
        → intent inference (why is this happening?)
        → specification check (does the user care about this?)
        → action (alert, adjust, anticipate, or do nothing)

Each step depends on the one before it. Raw sensor data never directly
triggers actions. Everything passes through context and intent first.

### Meta-Reasoner: Four Components

The agent reasons about the world. The Meta-Reasoner reasons about the
agent. It implements four components that together form the system's
autonomous intelligence cycle. The AV-derived design principles (Section
7) are not separate concerns --- they ARE the Meta-Reasoner's operating
framework.

**Curiosity** --- pulls toward the unknown. Flags unresolved
uncertainty. Treats not-knowing as the attractive force. Directional but
not targeted. Operational via: Shadow Mode (Pattern 2), Long Tail
Logging (Pattern 5). The Curiosity component surfaces what the system
has not seen before and what it cannot yet classify. Every novel input
is a question the system wants answered.

**Desire** --- pulls toward a known goal state. Direction and motivation
toward defined outcomes (home security, occupant safety, accurate
presence). Operational via: Fast Path / Slow Path Split (Pattern 3),
Explicit Degradation Modes (Pattern 4). The Desire component ensures the
system always knows what resolution looks like, even when operating at
reduced capability.

**Drive** --- the persistence mechanism. Keeps the system funded between
immediate rewards. Discipline in service of something larger.
Operational via: Dynamic Sensor Confidence Weighting (Pattern 1),
Structured Calibration Scenarios (Pattern 6). The Drive component
sustains system accuracy over time through calibration discipline and
trust management.

**Action** --- the expression mechanism. Converts resolved intent into
system behavior. Where Curiosity, Desire, and Drive are all pull forces,
Action is the output vector --- the moment the system stops observing
and starts changing state. Without it, the Meta-Reasoner is sentient but
inert. Drive points at Action; Action is what Drive is sustaining
toward. Operational via: output bus dispatch, state change commit,
MQTT publish.

System relationship: Curiosity surfaces what is unresolved. Desire
defines what resolution looks like. Drive sustains the system until
resolution. Action commits the resolved intent as system behavior.

### Learning Systems

Learning refines the world model over time. It does not change *how*
the system reasons --- it changes *what the system knows* about this
specific home.

**Routine modeling** --- Temporal patterns per person. Not just "when"
but sequences and transitions. The system learns that Alice goes
desk → kitchen → desk in the morning, but kitchen → living room in
the evening. Deviation from the sequence matters more than deviation
from the clock.

**Sensor reliability scoring** --- Dynamic per-person, per-room
weighting. The system discovers that WiFi CSI is excellent for
detecting Alice at his desk (consistent chair position, predictable
CSI shadow) but unreliable in the kitchen (movement variability, metal
appliances). Bayesian fusion weights shift automatically based on
demonstrated accuracy.

**Anomaly calibration** --- The threshold for "anomalous" narrows over
time as the system builds confidence in what is normal. Week 1: many
things look unusual. Month 3: only genuinely unusual things trigger
attention. False positives decline as pattern memory deepens.

**Composite identity scoring** --- Bayesian fusion across all identity
signals, weighted by learned reliability. Face recognition (high
confidence, low availability) + WiFi bio-signature (medium confidence,
high availability) + thermal silhouette (weak signal, always available)
= composite identity score that improves as each sensor's per-person
accuracy is calibrated.

### The Compounding Intelligence Moat

-   **Day 1:** Basic presence detection with first-principles reasoning.
    Physical model seeded, narrative begins, specifications active.
    The system is useful immediately.

-   **Week 1:** Routine patterns emerge. The system begins inferring
    intent. "He is heading to bed" becomes a prediction, not just an
    observation.

-   **Month 1:** Health baselines established. Sensor reliability scores
    calibrated per person per room. False positives declining. The
    narrative model carries meaningful history.

-   **Month 6:** Subtle deviations detectable --- a gradual resting
    heart rate increase, a shift in morning routine timing, a gait
    change. The system notices things the occupant might not.

Every day of operation makes the system smarter, more accurate, and
harder to replace. This timeline cannot be shortcut. A competitor
installing identical hardware starts at Day 1 --- the six months of
learned context, calibrated sensors, and refined baselines do not
transfer. This is the moat.

### Agent Integration

The sentinel agent (at any tier) interfaces with the intelligence layer
through MCP tools:

-   `get_current_narrative` --- retrieve the running causal story
-   `get_physical_model` --- query the spatial-relational map
-   `get_sensor_state` --- raw and interpreted current readings
-   `query_pattern_memory` --- "what is normal for this person/zone/time?"
-   `query_identity` --- composite identity score for a detected body
-   `update_specification` --- modify what the system cares about

The same MCP interface serves all agent tiers. A rule engine (Basic)
checks specifications against sensor state. A local LLM (Pro) reasons
over the full narrative. Claude (Elite) can explain its reasoning in
natural language and handle novel situations the other tiers cannot.

### Storage

All data remains local. No cloud, no subscriptions, no data leaving
the network.

-   **Working memory:** In-memory (Redis or native) on Pi/Jetson
-   **Episodic memory:** Time-series database (InfluxDB or SQLite with
    timestamps)
-   **Pattern memory:** Time-series database, aggregated from episodic
-   **Identity memory:** Relational database (SQLite), encrypted at rest
-   **Physical model:** JSON/YAML configuration, updated programmatically
-   **Narrative state:** In-memory with periodic snapshots to disk

Final storage technology selection (InfluxDB vs SQLite vs hybrid) will
be determined during implementation based on Pi 5 performance
characteristics and memory constraints.

# 7. AV-Derived Design Principles

Six design patterns from autonomous vehicle engineering transfer
directly to Sentinel. One does not. These patterns are not just
engineering patterns --- they are the operational expression of the
Meta-Reasoner's four components (Curiosity, Desire, Drive, Action).
Each pattern maps to one or more Meta-Reasoner components and is
implemented in `sentinel/av_principles.py`.

## 7.1 Dynamic Sensor Confidence Weighting (Drive Engine)

Sensors are weighted dynamically based on reading consistency, not just
failed/operational status. A degrading sensor is trusted less before it
fails completely. Implement in sentinel_fusion.py as a continuous
confidence score per sensor per zone, not a binary healthy/unhealthy
flag.

## 7.2 Shadow Mode (Curiosity Engine)

New detection models run silently alongside current models, logging
decisions without acting. A model is promoted to active only after
validation against ground truth. Applies to new identity algorithms,
behavioral baselines, and reasoning memory updates. Every model carries
a shadow mode flag.

## 7.3 Fast Path / Slow Path Split (Desire Engine)

The reflexive fast path (unknown presence → alert) is a separate
lightweight service independent of the agent. It cannot be blocked by
agent reasoning or Brain being offline. The fast path runs on every
node and on the Hub as a standalone process.

## 7.4 Explicit Degradation Modes (Desire Engine)

The system declares its operating mode rather than silently degrading:

  --------------------------------------------------------------------------
  **MODE**       **CONDITION**              **BEHAVIOR**
  -------------- -------------------------- --------------------------------
  **Full**       All nodes online, Brain    Full fusion, identity, narrative
                 running                    reasoning active

  **Degraded**   Brain offline              Nodes operate on local rules
                                            autonomously, lateral sync
                                            continues

  **Minimal**    Single node, no peers      Local detection only, alert on
                                            any unknown presence
  --------------------------------------------------------------------------

Transitions between modes are explicit state machine events, not
implicit fallbacks.

## 7.5 Long Tail Logging (Curiosity Engine)

Every novel input the system has not seen before is flagged, stored, and
reviewable. This is the foundation of system learning and vulnerability
awareness. Novel events feed into the reasoning log and are surfaced for
periodic review.

## 7.6 Structured Calibration Scenarios (Drive Engine)

Calibration is a test track, not just a baseline capture. Known
controlled inputs run as structured scenarios before deployment ---
walk-through patterns, multi-person scenes, known/unknown identity
tests, edge cases. Calibration validates system behavior, not just
sensor readings.

## 7.7 Pattern That Does Not Transfer

**Millisecond latency optimization** does not apply. Sentinel's threat
model operates on a slower timescale than autonomous driving. The system
can afford deliberation. Do not sacrifice accuracy for latency.

# 8. Products & Markets

## 8.1 Sentinel Secure

Identity-aware security that knows who is present, whether they belong,
and whether the body is real and alive. Layers over existing alarm
systems --- does not require ripping and replacing.

**Integration path with existing alarm infrastructure:**

-   Phase 1 --- Parallel operation alongside existing panel. Sentinel
    adds intelligence without touching the certified chain.

-   Phase 2 --- Envisalink / Alarm.com bridge. Sentinel reads panel
    state (armed/disarmed) and adds context to panel events.

-   Phase 3 --- Sentinel as panel replacement for new installs. UL
    certification path. Direct SIA DC-09 central station transmission.
    Long road, significant moat.

**Target markets:**

-   High net worth residential --- estates, privacy-conscious
    homeowners, repeat targets

-   Commercial --- server rooms, data centers, executive floors,
    boardrooms

-   Industrial --- restricted zones, clean rooms, critical
    infrastructure

-   Cannabis / dispensary --- high-value inventory, compliance
    requirements

## 8.2 Sentinel Living

Identity-aware home automation. The room knows who is in it and responds
to their identity and physiological state --- not a motion trigger, not
a manual scene. A room with intelligence about its occupant.

Example scenarios:

-   Alice enters office at 9pm, stress markers elevated → dim to
    2700K focus scene, DND on all devices, thermostat to preference

-   Bob and Alice both present in living room → negotiated shared
    scene, both preference profiles blended

-   Unknown person enters with authorized occupant, authorized occupant
    shows relaxed behavior → log unknown, no alert, monitor

-   Alice enters kitchen at 6am matching morning routine → coffee
    maker starts, news brief at preferred volume, wake-up lighting

*Compounding value: the system gets smarter with use. More time running
= deeper behavioral baseline = more accurate prediction. This is a moat
that deepens over time.*

**Target markets:**

-   High-end residential --- luxury builders, smart home integrators

-   Hospitality --- hotels (know occupancy without key cards), resorts

-   Senior living --- passive comfort automation, routine monitoring

## 8.3 Sentinel Health

The vitals data captured for identity --- breathing rate, stress index,
thermal baseline, activity state --- does not disappear. Redirected
toward wellness, it becomes a passive continuous health monitor with no
wearable required.

Capabilities:

-   Breathing rate monitoring --- continuous, passive, no contact

-   Stress index --- thermal + VOC + behavioral combined

-   Activity level and posture --- sedentary alerts, fall detection

-   Routine deviation --- \'Alice hasn\'t left bedroom by 10am\'
    alert

-   Illness indicators --- VOC metabolic profile, thermal pattern change

**Target markets:**

-   Senior living --- passive fall detection, wellness baseline, family
    alerts

-   Healthcare facilities --- patient monitoring without wearable
    compliance issues

-   Wellness / longevity --- biometric baseline tracking for
    health-conscious users

## 8.4 Sentinel Robotics --- Spatial Intelligence for Indoor Robots

Sentinel's world model is directly consumable by indoor robots via
MQTT and MCP. No architecture changes are required --- robots are simply
another consumer of the living world model that Sentinel already
maintains.

**Value proposition:** Robots today navigate blind. They build local
maps with onboard sensors but have no awareness of what is beyond their
line of sight. Sentinel gives any robot passive, through-wall, room-scale
spatial awareness --- who is where, what rooms are occupied, where
people are moving --- before the robot enters a room.

**Use cases:**

-   Robot vacuums --- avoid occupied rooms, clean when people leave,
    navigate to correct zones without random exploration

-   Delivery robots (indoor) --- route around people, find the recipient
    by identity, avoid congested zones

-   Assistive robots --- locate the person who needs help, detect falls
    or anomalies, navigate directly to them

-   Warehouse AMRs --- human-aware path planning, safety zones around
    detected workers, occupancy-based scheduling

**Integration:** Robots subscribe to Sentinel MQTT topics or call MCP
tools (get_zone_occupancy, get_world_model) for real-time spatial
context. The robot's own SLAM handles local navigation; Sentinel
provides the global awareness layer.

**Strategic value:** Nobody else is building passive through-wall
room-scale awareness as a service for robots. This widens the acquirer
pool significantly (iRobot, Amazon Astro, warehouse robotics companies)
without requiring any changes to the core platform.

# 9. What We Have vs. What We Need

## 9.1 Hardware --- Current State

  --------------------------------------------------------------------------
  **ITEM**                 **STATUS**   **NOTES**
  ------------------------ ------------ ------------------------------------
  NVIDIA Jetson Orin Nano  **✓ OWNED**  Hub compute --- sufficient for
  (<keep-host>)                            multi-node fusion

  ESP32-S3 nodes           **✓ OWNED**  CSI nodes deployed + Sentinel node
  (BAKODELOP x4)                        firmware built with LD2450 radar,
                                        BME688 env, SPH0645 mic, BLE/WiFi
                                        scanner. Hardware arrived March 13

  Mosquitto MQTT broker    **✓          On <keep-host> --- all sensor data will
                           RUNNING**    flow through this

  csi_bridge.py            **✓          WiFi CSI presence detection ---
                           RUNNING**    proven, in production

  Home Assistant           **✓          MQTT integration path exists
  integration              RUNNING**    

  HLK-LD2450 radar x4      **✓          \$14 x4 = \$56 --- received
                           RECEIVED**   March 13

  Topdon TC001 thermal     **✓          \$199 --- received March 13,
                           RECEIVED**   replaces Infiray P2 Pro,
                                        512x384 TISR, USB-C UVC,
                                        connected and working

  YDLIDAR X4PRO            **✓          \$80 --- arriving March 15,
                           ORDERED**    upgraded from X4

  RPi Camera v2 + IR       **✓          \$25 --- received March 12,
                           RECEIVED**   connected and working

  MEMS mic SPH0645 x1      **✓          \$9.48 --- arriving March 16,
                           ORDERED**    single mic (array expansion
                                        later)

  BME688 VOC sensor        **✓          \$25.70 --- WT-BME688, received
  (WT-BME688)              RECEIVED**   March 14
  --------------------------------------------------------------------------

## 9.2 Software --- Current State

  --------------------------------------------------------------------------
  **COMPONENT**            **STATUS**   **NOTES**
  ------------------------ ------------ ------------------------------------
  csi_bridge.py --- CSI    **✓ BUILT**  Proven on ESP32-S3 nodes, running as
  parsing + MQTT                        systemd service

  monday_agent.py ---      **✓ BUILT**  Demonstrates MCP pattern --- reuse
  Monday.com MCP                        for Sentinel MCP

  MQTT broker + HA         **✓          Infrastructure ready for Sentinel
  integration              RUNNING**    topics

  Calibration system       **✓ BUILT**  60-sec auto-baseline, 3-sigma
  (csi_bridge)                          presence / 5-sigma motion thresholds,
                                        EMA filter (α=0.05), slow-drift
                                        correction, MQTT recalibrate command,
                                        SQLite persistence to disk

  sentinel_fusion.py       **✓ BUILT**  Single-source CSI fusion pipeline,
                                        3-layer validation (health gate,
                                        plausibility, cross-sensor stub),
                                        temporal occupancy state machine,
                                        zone update hooks for memory arch

  sentinel_node.ino        **✓ BUILT**  7-layer sensor fusion firmware ---
  (ESP32-S3)                            LD2450 radar, BME688 env, SPH0645
                                        mic, BLE/WiFi scanner, dual output
                                        (UDP 10Hz + MQTT 1Hz)

  meta_reasoner/           **✓ BUILT**  Stage 1 stub --- three continuous
  service.py               (Stage 1)    questions, self-model, uncertainty
                                        classification, MQTT integration,
                                        insight generation interface

  av_principles.py         **✓ BUILT**  Six AV patterns as code --- trust
                                        decay curve, shadow runner, fast/slow
                                        split, degradation state machine, long
                                        tail logger, calibration scenario
                                        runner, Meta-Reasoner orchestrator

  brain/service.py +       **✓ BUILT**  Brain service + NarrativeEngine ---
  narrative.py                          world model, zone tracking, actor
                                        state transitions, narrative output

  adapters/                **✓ BUILT**  Legacy topic translation with
  csi_adapter.py                        confidence certificates

  schemas/messages.py      **✓ BUILT**  Complete message schema --- 4 layers,
                                        all MQTT messages as dataclasses

  config.py + topics.py    **✓ BUILT**  Configuration management + canonical
                                        MQTT topic hierarchy

  identity_engine.py       **✗ NEEDED** Profile enrollment, thermal + gait +
                                        VOC matching

  behavior_engine.py       **✗ NEEDED** Baseline learning, anomaly
                                        detection, routine modeling

  sentinel_mcp_server.py   **✗ NEEDED** MCP tool schema --- integration
                                        point for agent

  sentinel_agent.py        **✗ NEEDED** Reasoning layer --- rule engine
                                        first, LLM tier later

  digest_sync.py           **✗ NEEDED** Compressed world model sync
                                        between peers

  peer_channel.py          **✗ NEEDED** Lateral node-to-node MQTT
                                        peer channel handler

  ca_service.py            **✗ NEEDED** Local CA on <keep-host> --- node
                                        cert issuance and management
  --------------------------------------------------------------------------

# 10. Development Roadmap

## 10.1 Phase 1 --- Prove the Fusion (Personal Lab)

Goal: Validate that multi-modal fusion produces reliable
presence/identity confidence on real hardware before any product
investment.

-   ~~Order prototype sensor hardware~~ **DONE** --- \$470 total,
    all 7 sensor layers ordered, arriving March 12-16

-   ~~Build calibration system into csi_bridge.py~~ **DONE** --- 60-sec
    auto-baseline, 3-sigma/5-sigma thresholds, EMA + drift correction

-   Integrate LD2450 radar → MQTT --- validate classification vs CSI
    **(NEXT --- sentinel_node.ino firmware)**

-   Integrate Topdon TC001 thermal → identity baseline capture

-   Build sentinel_fusion.py --- first confidence scoring model

-   Validate: can the system distinguish Alice from Bob from
    unknown?

> *Nothing else matters until fusion is proven on real hardware in a
> real room. All product and business decisions follow from this
> validation.*

## 10.2 Phase 2 --- V1 Product

Goal: Sellable product for first 10-20 integrator deployments. Refine
based on real installs.

-   Custom PCB --- replaces discrete breakout boards, integrates MCU +
    sensor interfaces

-   Proper housing --- per-sensor apertures, PoE power, single cable
    install

-   Identity engine with enrollment workflow --- integrator-friendly
    setup

-   MCP server + agent (rule engine tier) --- Sentinel Basic

-   Home Assistant integration --- HACS custom component

-   Installer documentation and configuration tooling

## 10.3 Phase 3 --- Platform Scale

Goal: Volume manufacturing, channel distribution, software licensing
model.

-   Optimized BOM --- radar IC direct (not dev board), custom optical
    design

-   Volume manufacturing --- contract manufacturer, \$80-100/node at
    1000+ units

-   FCC / UL certification --- required for channel sales

-   Sentinel Pro --- local LLM (Ollama on Hub) agent tier

-   Sentinel Elite --- cloud LLM tier with explainable reasoning

-   Alarm panel replacement path --- UL listed, SIA DC-09 direct ---
    Phase 3 end goal

# 11. Business Model

## 11.1 Go-To-Market Channel

Security integrators are the primary channel --- same relationships as
the AI Takeoff product. Integrators install, configure, enroll
occupants, and maintain the customer relationship. Sentinel sells
hardware + software license. No direct end-customer touch required.

> *The integrator channel is already validated through existing
> relationships. This is not a new sales motion --- it is an extension
> of an existing one.*

## 11.2 Revenue Model

  ---------------------------------------------------------------------------
  **STREAM**      **MODEL**        **PRICE POINT**      **NOTES**
  --------------- ---------------- -------------------- ---------------------
  **Hardware**    One-time sale    **Retail             Margin on custom PCB
                                   \$299-399/node**     at volume

  **Software ---  One-time license **\$199/hub**        Rule engine, no LLM
  Basic**                                               

  **Software ---  Annual           **\$99/year/hub**    Local LLM, updates,
  Pro**           subscription                          support

  **Software ---  Annual           **\$299/year/hub**   Cloud LLM tier,
  Elite**         subscription                          premium support

  **Integrator    Hardware markup  **30-40% integrator  Standard AV/security
  margin**                         margin**             channel model
  ---------------------------------------------------------------------------

## 11.3 Competitive Moat

The defensible IP is the fusion engine trained across real deployments
--- not the hardware. Each layer of the moat compounds over time:

-   Fusion model trained on real multi-modal data --- not replicable
    without the same data

-   Behavioral baseline per occupant --- deepens with every day of
    operation

-   Integrator relationships --- switching cost increases with each
    enrolled property

-   Fully local / private --- genuinely differentiates from every
    cloud-dependent competitor

-   No subscription for core function --- removes primary objection in
    residential market

## 11.4 Comparable Market Landscape

  --------------------------------------------------------------------------------
  **COMPETITOR**   **CATEGORY**    **COST**          **CLOUD    **SENTINEL
                                                     DEP.**     ADVANTAGE**
  ---------------- --------------- ----------------- ---------- ------------------
  **Ring / Arlo /  Consumer camera \$100-300/cam +   Required   Identity,
  Nest**                           sub                          liveness, no
                                                                subscription

  **SimpliSafe /   Consumer alarm  \$200-500 +       Required   Passive ID,
  ADT**                            monitoring                   behavioral
                                                                intelligence

  **Verkada /      Enterprise      \$500-2k/cam +    Required   10x cheaper,
  Avigilon**       camera          sub                          local, multi-modal

  **Alarm.com**    Connected alarm \$30-60/mo        Required   Local, no monthly
                                                                fee, richer data

  **Home           Smart home      Free (hardware    Optional   Identity-aware,
  Assistant**                      cost)                        not just presence
  --------------------------------------------------------------------------------

# 12. Open Questions & Risks

## 12.1 Technical Risks

-   Thermal identity at range --- Topdon TC001 provides 512x384 TISR
    (256x192 native), improving vascular mapping range vs original P2
    Pro spec. Still needs validation beyond 3-4m.

-   VOC baseline stability --- BME688 VOC readings drift with humidity
    and temperature. Baseline normalization required.

-   LiDAR in complex environments --- furniture, pets, and reflective
    surfaces create false geometry. Requires robust filtering.

-   Multi-person scenes --- identity engine must reliably separate two
    people at 1m apart. Radar and LiDAR geometry fusion is key.

## 12.2 Regulatory / Legal

-   Biometric data storage --- thermal vascular maps and gait signatures
    are biometric data. BIPA (Illinois), CCPA, and GDPR have specific
    requirements. Local-only storage is the correct architectural choice
    and the primary mitigation.

-   UL certification for alarm replacement --- Phase 3 path requires
    formal UL listing. Significant time and cost investment.

-   FCC certification --- required for any RF emitting device (radar
    nodes) sold commercially.

## 12.3 Key Decisions Needed

-   First pilot customer --- which integrator gets the V1 prototype?
    This decision drives the initial use case focus.

-   Sentinel Living vs Sentinel Secure as lead product --- security has
    faster sales cycle but automation has larger total market.

-   PCB design partner --- identify EE resource for custom board design
    before Phase 2.

-   Name finalization --- Sentinel works. Consider whether a less
    descriptive brand name provides better IP protection.
