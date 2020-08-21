from cereal import car
from common.realtime import DT_CTRL
from common.numpy_fast import clip, interp
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfa_mfa, \
                                             create_scc11, create_scc12
from selfdrive.car.hyundai.interface import GearShifter
from selfdrive.car.hyundai.values import Buttons, SteerLimitParams, CAR, FEATURES
from opendbc.can.packer import CANPacker
from selfdrive.config import Conversions as CV
from selfdrive.controls.lib.longcontrol import LongCtrlState
from selfdrive.controls.lib.pathplanner import LANE_CHANGE_SPEED_MIN

VisualAlert = car.CarControl.HUDControl.VisualAlert

# Accel Hard limits
ACCEL_HYST_GAP = 0.0  # don't change accel command for small oscilalitons within this value
ACCEL_MAX = 3.  # 1.5 m/s2
ACCEL_MIN = -3.5  # 3   m/s2
ACCEL_SCALE = 1.

def accel_hysteresis(accel, accel_steady):

  # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
  if accel > accel_steady + ACCEL_HYST_GAP:
    accel_steady = accel - ACCEL_HYST_GAP
  elif accel < accel_steady - ACCEL_HYST_GAP:
    accel_steady = accel + ACCEL_HYST_GAP
  accel = accel_steady

  return accel, accel_steady

def process_hud_alert(enabled, fingerprint, visual_alert, left_lane,
                      right_lane, left_lane_depart, right_lane_depart, lkas_button):

  sys_warning = (visual_alert == VisualAlert.steerRequired) and lkas_button
  if sys_warning:
      sys_warning = 4 if fingerprint in [CAR.HYUNDAI_GENESIS, CAR.GENESIS_G90, CAR.GENESIS_G80] else 3

  if enabled or sys_warning:
      sys_state = 3
  else:
      sys_state = lkas_button

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if left_lane_depart and lkas_button:
    left_lane_warning = 1 if fingerprint in [CAR.HYUNDAI_GENESIS, CAR.GENESIS_G90, CAR.GENESIS_G80] else 2
  if right_lane_depart and lkas_button:
    right_lane_warning = 1 if fingerprint in [CAR.HYUNDAI_GENESIS, CAR.GENESIS_G90, CAR.GENESIS_G80] else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning


class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.cp_opcontrol = CP.openpilotLongitudinalControl
    self.steermaxLimit = int(CP.steermaxLimit)
    self.packer = CANPacker(dbc_name)
    self.accel_steady = 0
    self.steer_rate_limited = False
    self.resume_cnt = 0
    self.last_resume_frame = 0
    self.last_lead_distance = 0
    self.longcontrol = False
    self.lead_visible = False
    self.lead_debounce = 0
    self.apply_accel_last = 0
    self.gapsettingdance = 2
    self.gapcount = 0
    self.acc_paused_due_brake = False
    self.acc_paused = False
    self.prev_acc_paused_due_brake = False
    self.manual_steering = False
    self.manual_steering_timer = 0

  def update(self, enabled, CS, frame, actuators, pcm_cancel_cmd, visual_alert,
             left_lane, right_lane, left_lane_depart, right_lane_depart, set_speed, lead_visible):

    # *** compute control surfaces ***

    if self.car_fingerprint in FEATURES["send_lfa_mfa"]:
      self.lfa_available = True
    else:
      self.lfa_available = False

    if lead_visible:
      self.lead_visible = True
      self.lead_debounce = 50
    elif self.lead_debounce > 0:
      self.lead_debounce -= 1
    else:
      self.lead_visible = lead_visible

    # gas and brake
    apply_accel = actuators.gas - actuators.brake

    apply_accel = clip(apply_accel * ACCEL_SCALE, ACCEL_MIN, ACCEL_MAX)

    # Steering Torque
    updated_SteerLimitParams = SteerLimitParams
    updated_SteerLimitParams.STEER_MAX = self.steermaxLimit

    new_steer = actuators.steer * updated_SteerLimitParams.STEER_MAX
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, updated_SteerLimitParams)
    self.steer_rate_limited = new_steer != apply_steer

    # disable if steer angle reach 90 deg, otherwise mdps fault in some models
    lkas_active = enabled and abs(CS.out.steeringAngle) < 90.

    # fix for Genesis hard fault at low speed
    if CS.out.vEgo < 55 * CV.KPH_TO_MS and self.car_fingerprint == CAR.HYUNDAI_GENESIS and not CS.mdps_bus:
      lkas_active = False

    if enabled and CS.out.steeringPressed and CS.out.vEgo < LANE_CHANGE_SPEED_MIN and (CS.out.leftBlinker or CS.out.rightBlinker):
      self.manual_steering_timer += 1
      if self.manual_steering_timer > 20:
         self.manual_steering = True
         self.manual_steering_timer = 20
    elif (self.manual_steering and not CS.out.leftBlinker and not CS.out.rightBlinker) or not CS.out.vEgo < LANE_CHANGE_SPEED_MIN or not enabled:
      self.manual_steering = False

    if self.manual_steering:
      lkas_active = False

    if not lkas_active:
      apply_steer = 0

    if (CS.cancel_button_count == 3) and not CS.brakeUnavailable and self.cp_opcontrol:
      self.longcontrol = not self.longcontrol

    if self.longcontrol:
      self.gapcount += 1
      if self.gapcount == 50 and self.gapsettingdance == 2:
        self.gapsettingdance = 1
        self.gapcount = 0
      elif self.gapcount == 50 and self.gapsettingdance == 1:
        self.gapsettingdance = 4
        self.gapcount = 0
      elif self.gapcount == 50 and self.gapsettingdance == 4:
        self.gapsettingdance = 3
        self.gapcount = 0
      elif self.gapcount == 50 and self.gapsettingdance == 3:
        self.gapsettingdance = 2
        self.gapcount = 0

    self.apply_accel_last = apply_accel
    self.apply_steer_last = apply_steer

    sys_warning, sys_state, left_lane_warning, right_lane_warning =\
      process_hud_alert(enabled, self.car_fingerprint, visual_alert,
                        left_lane, right_lane, left_lane_depart, right_lane_depart, CS.lkas_button_on)

    clu11_speed = CS.clu11["CF_Clu_Vanz"]
    enabled_speed = 38 if CS.is_set_speed_in_mph  else 60
    if clu11_speed > enabled_speed or not lkas_active or CS.out.gearShifter == GearShifter.reverse:
      enabled_speed = clu11_speed

    if CS.is_set_speed_in_mph:
      set_speed *= CV.MS_TO_MPH
    else:
      set_speed *= CV.MS_TO_KPH

    can_sends = []
    can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active,
                                   CS.lkas11, sys_warning, sys_state, enabled,
                                   left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, self.lfa_available, 0))

    if CS.mdps_bus: # send lkas11 bus 1 if mdps
      can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active,
                                   CS.lkas11, sys_warning, sys_state, enabled,
                                   left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, self.lfa_available, 1))

      can_sends.append(create_clu11(self.packer, frame, CS.mdps_bus, CS.clu11, Buttons.NONE, enabled_speed))

    if pcm_cancel_cmd and not self.cp_opcontrol:
      can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, Buttons.CANCEL, clu11_speed))
    elif CS.out.cruiseState.standstill and not self.longcontrol:
      # SCC won't resume anyway when the lead distace is less than 3.7m
      # send resume at a max freq of 5Hz
      if CS.lead_distance > 3.7 and (frame - self.last_resume_frame)*DT_CTRL > 0.2:
        can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, Buttons.RES_ACCEL, clu11_speed))
        self.last_resume_frame = frame

    self.prev_acc_paused_due_brake = self.acc_paused_due_brake

    if self.longcontrol and CS.rawcruiseStateavailable and (CS.out.brakePressed or CS.out.brakeHold or
                                                            (CS.cruise_buttons == 4) or (CS.out.gasPressed and
                                                                                         not (CS.cruise_buttons == 1 or
                                                                                              CS.cruise_buttons == 2))):
      self.acc_paused = True
      self.acc_paused_due_brake = (CS.cruise_buttons == 4) or (CS.out.brakePressed and (2. < CS.out.vEgo < 15.)) or self.acc_paused_due_brake
    elif CS.cruise_buttons == 1 or CS.cruise_buttons == 2 or (not self.acc_paused_due_brake) or (not self.longcontrol):
      self.acc_paused = False
      self.acc_paused_due_brake = False

    self.acc_standstill = True if (LongCtrlState.stopping and CS.out.standstill) else False

    # send scc to car if longcontrol enabled and SCC not on bus 0 or ont live
    if CS.scc_bus == 2:
          can_sends.append(create_scc12(self.packer, apply_accel, enabled,
                                    self.acc_standstill, self.acc_paused,
                                    CS.out.cruiseMainbutton,
                                    CS.scc12, self.longcontrol))

          can_sends.append(create_scc11(self.packer, enabled,
                                    set_speed, self.lead_visible,
                                    self.gapsettingdance,
                                    CS.out.standstill, CS.scc11, self.longcontrol))

    # 20 Hz LFA MFA message
    if frame % 5 == 0 and self.lfa_available:
      can_sends.append(create_lfa_mfa(self.packer, frame, enabled))

    return can_sends
