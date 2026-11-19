"""The paint plant: equipment, pipe headers, and the shared resources.

Layout (all batches use the same physical plant)::

    RM-1 resin  --V-RM1--+
    RM-2 fill   --V-RM2--+-- LETDOWN (header) --V-LD--+
    RM-3 add A  --V-RM3--+                            |
    RM-4 add B  --V-RM4--+                            |
                                                      |
    REACTOR-1 / REACTOR-2 (reaction vessels) <--V-R1--+
                                   |
                                   +--V-R1OUT-- LETDOWN2 --V-LD2--+
                                                                 |
    PREMIX-1 / PREMIX-2 (let-down tanks) <--V-PM1IN/PM2IN <------+
                                   |
                                   +--V-PM1OUT/PM2OUT -- MILLLINE --V-MILLIN--+
                                                                             |
    FINISH-1 / FINISH-2 (let-down / thinning tanks) <--V-F1IN/F2IN <--------+
                                   |
                                   +--V-F1OUT/F2OUT -- FILLLINE (to filling)

    Product tanks hold finished paint; the waste vessel takes drains.

Shared (exclusive-use) resources are the line segments: every transfer
needs ``Acquire([segment], priority=...)`` and a matching ``Release``.
``FILLLINE`` is the line that caused the original incident: two batches
tried to pump to the filler at once.
"""

from __future__ import annotations

from .sim import Plant


def build_plant() -> Plant:
    p = Plant()

    # -- raw material tanks ------------------------------------------------
    p.add_tank("RM-1", capacity_kg=20000, agitator_ms=1000)
    p.add_tank("RM-2", capacity_kg=20000, agitator_ms=1000)
    p.add_tank("RM-3", capacity_kg=8000, agitator_ms=1000)
    p.add_tank("RM-4", capacity_kg=8000, agitator_ms=1000)

    # -- process vessels ---------------------------------------------------
    p.add_tank("REACTOR-1", capacity_kg=6000, agitator_ms=4000,
               heater_w=120000)
    p.add_tank("REACTOR-2", capacity_kg=6000, agitator_ms=4000,
               heater_w=120000)
    p.add_tank("PREMIX-1", capacity_kg=6000, agitator_ms=3000)
    p.add_tank("PREMIX-2", capacity_kg=6000, agitator_ms=3000)
    p.add_tank("FINISH-1", capacity_kg=8000, agitator_ms=3000)
    p.add_tank("FINISH-2", capacity_kg=8000, agitator_ms=3000)
    p.tanks["REACTOR-1"].max_temp_c = 110.0
    p.tanks["REACTOR-2"].max_temp_c = 110.0

    # -- finished product / waste -----------------------------------------
    p.add_tank("PRODUCT", capacity_kg=40000)
    waste = p.add_tank("WASTE", capacity_kg=40000)
    p.waste = waste

    # -- raw material valves (stroke 2.5 s) -------------------------------
    for tag in ("RM-1", "RM-2", "RM-3", "RM-4"):
        p.add_valve(f"V-{tag}", travel_ms=2500)

    # -- header LETDOWN: raw-material header into the reactors ------------
    p.add_line("LETDOWN")
    p.add_valve("V-LD-R1", travel_ms=2500)
    p.add_valve("V-LD-R2", travel_ms=2500)
    p.add_pump("P-LD", flow_kg_s=35.0, ramp_up_ms=2000, ramp_down_ms=800)

    # -- additive header: RM-3 / RM-4 straight into the reactors ----------
    p.add_line("ADD-LINE")
    p.add_valve("V-ADD-R1", travel_ms=2500)
    p.add_valve("V-ADD-R2", travel_ms=2500)
    p.add_pump("P-ADD", flow_kg_s=12.0, ramp_up_ms=1000, ramp_down_ms=800)

    # -- reactor out header ------------------------------------------------
    p.add_line("REACT-OUT")
    p.add_valve("V-R1-OUT", travel_ms=2500)
    p.add_valve("V-R2-OUT", travel_ms=2500)
    p.add_pump("P-RO", flow_kg_s=30.0, ramp_up_ms=1500, ramp_down_ms=800)

    # -- premix header -----------------------------------------------------
    p.add_valve("V-PM1-IN", travel_ms=2500)
    p.add_valve("V-PM2-IN", travel_ms=2500)
    p.add_valve("V-PM1-OUT", travel_ms=2500)
    p.add_valve("V-PM2-OUT", travel_ms=2500)

    # -- mill line ---------------------------------------------------------
    p.add_line("MILL-LINE")
    p.add_valve("V-MILL-IN", travel_ms=2500)
    p.add_valve("V-MILL-OUT", travel_ms=2500)
    p.add_pump("P-MILL", flow_kg_s=20.0, ramp_up_ms=1500, ramp_down_ms=1000)
    p.add_machine("MILL-1", spin_up_ms=5000, spin_down_ms=4000)
    p.add_machine("MILL-2", spin_up_ms=5000, spin_down_ms=4000)

    # -- finish tanks / filler line ---------------------------------------
    p.add_valve("V-F1-IN", travel_ms=2500)
    p.add_valve("V-F2-IN", travel_ms=2500)
    p.add_valve("V-F1-OUT", travel_ms=2500)
    p.add_valve("V-F2-OUT", travel_ms=2500)
    p.add_line("FILL-LINE")
    p.add_valve("V-FILL", travel_ms=3000)
    p.add_pump("P-FILL", flow_kg_s=25.0, ramp_up_ms=1500, ramp_down_ms=800)

    # -- waste line --------------------------------------------------------
    p.add_line("WASTE-LINE")
    p.add_valve("V-WASTE", travel_ms=2000)
    return p


#: resources a recipe may acquire (line segments are the shared ones)
SHARED_RESOURCES = ("LETDOWN", "ADD-LINE", "REACT-OUT", "MILL-LINE",
                    "FILL-LINE", "WASTE-LINE")
