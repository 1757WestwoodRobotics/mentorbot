from math import cos, sin
from commands2 import SubsystemBase
from wpilib import Encoder, PWMVictorSPX, RobotBase, Timer, SmartDashboard
from ctre import (
    AbsoluteSensorRange,
    CANCoder,
    ControlMode,
    ErrorCode,
    SensorInitializationStrategy,
    WPI_TalonFX,
)
from rev import CANSparkMax, REVLibError
from navx import AHRS
from wpimath.geometry import Rotation2d, Pose2d
from wpimath.kinematics import (
    ChassisSpeeds,
    SwerveModuleState,
    SwerveDrive4Kinematics,
    SwerveDrive4Odometry,
)
from wpimath.filter import SlewRateLimiter
from enum import Enum, auto
from typing import Tuple

from wpimath.kinematics._kinematics import SwerveModulePosition
import constants
from util.convenietmath import optimizeAngle


class SwerveModule:
    def __init__(self, name: str) -> None:
        self.name = name

    def getSwerveAngle(self) -> Rotation2d:
        raise NotImplementedError("Must be implemented by subclass")

    def setSwerveAngle(self, swerveAngle: Rotation2d) -> None:
        raise NotImplementedError("Must be implemented by subclass")

    def setSwerveAngleTarget(self, swerveAngleTarget: Rotation2d) -> None:
        raise NotImplementedError("Must be implemented by subclass")

    def getWheelLinearVelocity(self) -> float:
        raise NotImplementedError("Must be implemented by subclass")

    def setWheelLinearVelocityTarget(self, wheelLinearVelocityTarget: float) -> None:
        raise NotImplementedError("Must be implemented by subclass")

    def reset(self) -> None:
        raise NotImplementedError("Must be implemented by subclass")

    def optimizedAngle(self, targetAngle: Rotation2d) -> Rotation2d:
        return optimizeAngle(self.getSwerveAngle(), targetAngle)

    def getState(self) -> SwerveModuleState:
        return SwerveModuleState(
            self.getWheelLinearVelocity(),
            self.getSwerveAngle(),
        )

    def getWheelTotalPosition(self) -> float:
        raise NotImplementedError("Must be implemented by subclass")

    def getPosition(self) -> SwerveModulePosition:
        return SwerveModulePosition(self.getWheelTotalPosition(), self.getSwerveAngle())

    def applyState(self, state: SwerveModuleState) -> None:
        optimizedState = SwerveModuleState.optimize(state, self.getSwerveAngle())

        self.setWheelLinearVelocityTarget(optimizedState.speed)
        if (
            abs(optimizedState.speed) >= constants.kMinWheelLinearVelocity
        ):  # prevent unneccisary movement for what would otherwise not move the robot
            optimizedAngle = self.optimizedAngle(optimizedState.angle)
            self.setSwerveAngleTarget(optimizedAngle)


class PWMSwerveModule(SwerveModule):
    """
    Implementation of SwerveModule designed for ease of simulation:
        wheelMotor: 1:1 gearing with wheel
        swerveMotor: 1:1 gearing with swerve
        wheelEncoder: wheel distance (meters)
        swerveEncoder: swerve angle (radians)
    """

    def __init__(
        self,
        name: str,
        wheelMotor: PWMVictorSPX,
        swerveMotor: PWMVictorSPX,
        wheelEncoder: Encoder,
        swerveEncoder: Encoder,
    ) -> None:
        SwerveModule.__init__(self, name)
        self.wheelMotor = wheelMotor
        self.swerveMotor = swerveMotor
        self.wheelEncoder = wheelEncoder
        self.swerveEncoder = swerveEncoder

        self.wheelEncoder.setDistancePerPulse(1 / constants.kWheelEncoderPulsesPerMeter)
        self.swerveEncoder.setDistancePerPulse(
            1 / constants.kSwerveEncoderPulsesPerRadian
        )

    def getSwerveAngle(self) -> Rotation2d:
        return Rotation2d(self.swerveEncoder.getDistance())

    def setSwerveAngleTarget(self, swerveAngleTarget: Rotation2d) -> None:
        swerveError = swerveAngleTarget.radians() - self.swerveEncoder.getDistance()
        swerveErrorClamped = min(max(swerveError, -1), 1)
        self.swerveMotor.set(swerveErrorClamped)

    def getWheelLinearVelocity(self) -> float:
        return self.wheelEncoder.getRate()

    def getWheelTotalPosition(self) -> float:
        return self.wheelEncoder.getDistance()

    def setWheelLinearVelocityTarget(self, wheelLinearVelocityTarget: float) -> None:
        speedFactor = wheelLinearVelocityTarget / constants.kMaxWheelLinearVelocity
        speedFactorClamped = min(max(speedFactor, -1), 1)
        self.wheelMotor.set(speedFactorClamped)

    def reset(self) -> None:
        pass


class REVSwerveModule(SwerveModule):
    """
    Implementation of SwerveModule for the SDS swerve modules
    https://www.swervedrivespecialties.com/collections/kits/products/mk4-swerve-module
        driveMotor: REV NEO Brushless Motor (with built-in encoder) connected to a SparkMax over CAN attached to wheel through gearing
        steerMotor: REV NEO Brushless Motor (with built-in encoder) connected to a SparkMax over CAN attached to swerve through gearing
        swerveEncoder: CANCoder
    """

    def __init__(
        self,
        name: str,
        driveMotor: CANSparkMax,
        driveMotorInverted: bool,
        steerMotor: CANSparkMax,
        steerMotorInverted: bool,
        swerveEncoder: CANCoder,
        swerveEncoderOffset: float,
    ) -> None:
        SwerveModule.__init__(self, name)
        self.driveMotor = driveMotor
        self.driveMotorEncover = self.driveMotor.getEncoder()
        self.driveMotorController = self.driveMotor.getPIDController()
        self.driveMotorInverted = driveMotorInverted
        self.steerMotor = steerMotor
        self.steerMotorEncoder = self.steerMotor.getEncoder()
        self.steerMotorController = self.steerMotor.getPIDController()
        self.steerMotorInverted = steerMotorInverted
        self.swerveEncoder = swerveEncoder
        self.swerveEncoderOffset = swerveEncoderOffset

        def ctreCheckError(name: str, errorCode: ErrorCode) -> bool:
            if (errorCode is not None) and (errorCode != ErrorCode.OK):
                print("ERROR: {}: {}".format(name, errorCode))
                return False
            return True

        def revCheckError(name: str, errorCode: REVLibError) -> bool:
            if errorCode is not None and errorCode != REVLibError.kOk:
                print("ERROR: {}: {}".format(name, errorCode))
                return False
            return True

        print("Initializing swerve module: {}".format(self.name))
        print(
            "   Configuring swerve encoder: CAN ID: {}".format(
                self.swerveEncoder.getDeviceNumber()
            )
        )
        if not ctreCheckError(
            "configFactoryDefault",
            self.swerveEncoder.configFactoryDefault(
                constants.kConfigurationTimeoutLimit
            ),
        ):
            return
        if not ctreCheckError(
            "configSensorInitializationStrategy",
            self.swerveEncoder.configSensorInitializationStrategy(
                SensorInitializationStrategy.BootToAbsolutePosition,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "configMagnetOffset",
            self.swerveEncoder.configMagnetOffset(
                -1 * self.swerveEncoderOffset,  # invert the offset to zero the encoder
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "configAbsoluteSensorRange",
            self.swerveEncoder.configAbsoluteSensorRange(
                AbsoluteSensorRange.Signed_PlusMinus180,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "setPositionToAbsolute",
            self.swerveEncoder.setPositionToAbsolute(
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        print("   ... Done")

        print(
            "   Configuring drive motor: CAN ID: {}".format(
                self.driveMotor.getDeviceId()
            )
        )
        if not revCheckError(
            "restoreFactoryDefaults", self.driveMotor.restoreFactoryDefaults()
        ):
            return
        self.driveMotor.setInverted(self.driveMotorInverted)
        if not revCheckError(
            "config_kP",
            self.driveMotorController.setP(
                constants.kDrivePGain, constants.kDrivePIDSlot
            ),
        ):
            return
        if not revCheckError(
            "config_kI",
            self.driveMotorController.setI(
                constants.kDriveIGain, constants.kDrivePIDSlot
            ),
        ):
            return
        if not revCheckError(
            "config_kD",
            self.driveMotorController.setD(
                constants.kDriveDGain, constants.kDrivePIDSlot
            ),
        ):
            return
        if not revCheckError(
            "config_PosConvert",
            self.driveMotorEncover.setPositionConversionFactor(
                constants.kSwerveMetersPerDriveEncoderRevolution
            ),
        ):
            return
        if not revCheckError(
            "config_VelConvert",
            self.driveMotorEncover.setVelocityConversionFactor(
                constants.kSwerveDriveEncoderRPMperMPS
            ),
        ):
            return
        print("   ... Done")

        print(
            "   Configuring steer motor: CAN ID: {}".format(
                self.steerMotor.getDeviceId()
            )
        )
        if not revCheckError(
            "restoreFactoryDefaults", self.steerMotor.restoreFactoryDefaults()
        ):
            return
        self.steerMotor.setInverted(self.steerMotorInverted)
        if not revCheckError(
            "config_kP",
            self.steerMotorController.setP(
                constants.kSteerPGain, constants.kSteerPIDSlot
            ),
        ):
            return
        if not revCheckError(
            "config_kI",
            self.steerMotorController.setI(
                constants.kSteerIGain, constants.kSteerPIDSlot
            ),
        ):
            return
        if not revCheckError(
            "config_kD",
            self.steerMotorController.setD(
                constants.kSteerDGain, constants.kSteerPIDSlot
            ),
        ):
            return
        if not revCheckError(
            "config_PosConvert",
            self.steerMotorEncoder.setPositionConversionFactor(
                constants.kSwerveMotorDegreesPerEncoderRev
            ),
        ):
            return
        if not revCheckError(
            "config_VelConvert",
            self.steerMotorEncoder.setVelocityConversionFactor(
                constants.kSwerveMotorDegreesPerEncoderRev / 60
            ),
        ):
            return
        print("   ... Done")

    # the following functions use helpful scaling done on the motor encoders to make the units nicer
    def getSwerveAngle(self) -> Rotation2d:
        steerEncoderDegrees = self.steerMotorEncoder.getPosition()
        return Rotation2d.fromDegrees(steerEncoderDegrees)

    def setSwerveAngle(self, swerveAngle: Rotation2d) -> None:
        steerEncoderDegrees = swerveAngle.degrees()
        self.steerMotorEncoder.setPosition(steerEncoderDegrees)

    def setSwerveAngleTarget(self, swerveAngleTarget: Rotation2d) -> None:
        steerAngleDegrees = swerveAngleTarget.degrees()
        self.steerMotorController.setReference(
            steerAngleDegrees, CANSparkMax.ControlType.kPosition
        )

    def getWheelTotalPosition(self) -> float:
        return self.driveMotorEncover.getPosition()

    def getWheelLinearVelocity(self) -> float:
        """meters / second"""
        driveEncoderRPM = self.driveMotorEncover.getVelocity()
        return driveEncoderRPM

    def setWheelLinearVelocityTarget(self, wheelLinearVelocityTarget: float) -> None:
        pass
        self.driveMotorController.setReference(
            wheelLinearVelocityTarget, CANSparkMax.ControlType.kVelocity
        )

    def reset(self) -> None:
        swerveEncoderAngle = (
            self.swerveEncoder.getAbsolutePosition() * constants.kRadiansPerDegree
        )
        self.setSwerveAngle(Rotation2d(swerveEncoderAngle))


class CTRESwerveModule(SwerveModule):
    """
    Implementation of SwerveModule for the SDS swerve modules
    https://www.swervedrivespecialties.com/collections/kits/products/mk4-swerve-module
        driveMotor: Falcon 500 Motor (with built-in encoder) attached to wheel through gearing
        steerMotor: Falcon 500 Motor (with built-in encoder) attached to swerve through gearing
        swerveEncoder: CANCoder
    """

    def __init__(
        self,
        name: str,
        driveMotor: WPI_TalonFX,
        driveMotorInverted: bool,
        steerMotor: WPI_TalonFX,
        steerMotorInverted: bool,
        swerveEncoder: CANCoder,
        swerveEncoderOffset: float,
    ) -> None:
        SwerveModule.__init__(self, name)
        self.driveMotor = driveMotor
        self.driveMotorInverted = driveMotorInverted
        self.steerMotor = steerMotor
        self.steerMotorInverted = steerMotorInverted
        self.swerveEncoder = swerveEncoder
        self.swerveEncoderOffset = swerveEncoderOffset

        def ctreCheckError(name: str, errorCode: ErrorCode) -> bool:
            if (errorCode is not None) and (errorCode != ErrorCode.OK):
                print("ERROR: {}: {}".format(name, errorCode))
                return False
            return True

        print("Initializing swerve module: {}".format(self.name))
        print(
            "   Configuring swerve encoder: CAN ID: {}".format(
                self.swerveEncoder.getDeviceNumber()
            )
        )
        if not ctreCheckError(
            "configFactoryDefault",
            self.swerveEncoder.configFactoryDefault(
                constants.kConfigurationTimeoutLimit
            ),
        ):
            return
        if not ctreCheckError(
            "configSensorInitializationStrategy",
            self.swerveEncoder.configSensorInitializationStrategy(
                SensorInitializationStrategy.BootToAbsolutePosition,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "configMagnetOffset",
            self.swerveEncoder.configMagnetOffset(
                -1 * self.swerveEncoderOffset,  # invert the offset to zero the encoder
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "configAbsoluteSensorRange",
            self.swerveEncoder.configAbsoluteSensorRange(
                AbsoluteSensorRange.Signed_PlusMinus180,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "setPositionToAbsolute",
            self.swerveEncoder.setPositionToAbsolute(
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        print("   ... Done")
        print(
            "   Configuring drive motor: CAN ID: {}".format(
                self.driveMotor.getDeviceID()
            )
        )
        if not ctreCheckError(
            "configFactoryDefault",
            self.driveMotor.configFactoryDefault(constants.kConfigurationTimeoutLimit),
        ):
            return
        # config = TalonFXConfiguration()
        # if not ctreCheckError(
        #     "getAllConfigs",
        #     self.driveMotor.getAllConfigs(config, constants.kConfigurationTimeoutLimit),
        # ):
        #     return
        # else:
        #     print("   Config:\n{}".format(config.toString()))
        self.driveMotor.setInverted(self.driveMotorInverted)
        if not ctreCheckError(
            "config_kP",
            self.driveMotor.config_kP(
                constants.kDrivePIDSlot,
                constants.kDrivePGain,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "config_kI",
            self.driveMotor.config_kI(
                constants.kDrivePIDSlot,
                constants.kDriveIGain,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "config_kD",
            self.driveMotor.config_kD(
                constants.kDrivePIDSlot,
                constants.kDriveDGain,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        print("   ... Done")

        print(
            "   Configuring steer motor: CAN ID: {}".format(
                self.steerMotor.getDeviceID()
            )
        )
        if not ctreCheckError(
            "configFactoryDefault",
            self.steerMotor.configFactoryDefault(constants.kConfigurationTimeoutLimit),
        ):
            return
        # config = TalonFXConfiguration()
        # if not ctreCheckError(
        #     "getAllConfigs",
        #     self.driveMotor.getAllConfigs(config, constants.kConfigurationTimeoutLimit),
        # ):
        #     return
        # else:
        #     print("   Config:\n{}".format(config.toString()))
        self.steerMotor.setInverted(self.steerMotorInverted)
        if not ctreCheckError(
            "config_kP",
            self.steerMotor.config_kP(
                constants.kSteerPIDSlot,
                constants.kSteerPGain,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "config_kI",
            self.steerMotor.config_kI(
                constants.kSteerPIDSlot,
                constants.kSteerIGain,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        if not ctreCheckError(
            "config_kD",
            self.steerMotor.config_kD(
                constants.kSteerPIDSlot,
                constants.kSteerDGain,
                constants.kConfigurationTimeoutLimit,
            ),
        ):
            return
        print("   ... Done")

        print("... Done")

    def getSwerveAngle(self) -> Rotation2d:
        steerEncoderPulses = self.steerMotor.getSelectedSensorPosition()
        swerveAngle = steerEncoderPulses / constants.kSwerveEncoderPulsesPerRadian
        # print("Steer[{}]: {}".format(self.steerMotor.getDeviceID(), swerveAngle))
        return Rotation2d(swerveAngle)

    def setSwerveAngle(self, swerveAngle: Rotation2d) -> None:
        steerEncoderPulses = (
            swerveAngle.radians() * constants.kSwerveEncoderPulsesPerRadian
        )
        self.steerMotor.setSelectedSensorPosition(steerEncoderPulses)

    def setSwerveAngleTarget(self, swerveAngleTarget: Rotation2d) -> None:
        steerEncoderPulsesTarget = (
            swerveAngleTarget.radians() * constants.kSwerveEncoderPulsesPerRadian
        )
        self.steerMotor.set(ControlMode.Position, steerEncoderPulsesTarget)

    def getWheelLinearVelocity(self) -> float:
        driveEncoderPulsesPerSecond = (
            self.driveMotor.getSelectedSensorVelocity()
            * constants.k100MillisecondsPerSecond
        )
        wheelLinearVelocity = (
            driveEncoderPulsesPerSecond / constants.kWheelEncoderPulsesPerMeter
        )
        return wheelLinearVelocity

    def setWheelLinearVelocityTarget(self, wheelLinearVelocityTarget: float) -> None:
        driveEncoderPulsesPerSecond = (
            wheelLinearVelocityTarget * constants.kWheelEncoderPulsesPerMeter
        )
        self.driveMotor.set(
            ControlMode.Velocity,
            driveEncoderPulsesPerSecond / constants.k100MillisecondsPerSecond,
        )

    def reset(self) -> None:
        swerveEncoderAngle = (
            self.swerveEncoder.getAbsolutePosition() * constants.kRadiansPerDegree
        )
        self.setSwerveAngle(Rotation2d(swerveEncoderAngle))


class DriveSubsystem(SubsystemBase):
    class CoordinateMode(Enum):
        RobotRelative = auto()
        FieldRelative = auto()

    def __init__(self) -> None:
        SubsystemBase.__init__(self)
        self.setName(__class__.__name__)

        self.xVelDamp = SlewRateLimiter(4.2)
        self.yVelDamp = SlewRateLimiter(4.2)

        if RobotBase.isReal():
            self.frontLeftModule = REVSwerveModule(
                constants.kFrontLeftModuleName,
                CANSparkMax(
                    constants.kFrontLeftDriveMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kFrontLeftDriveInverted,
                CANSparkMax(
                    constants.kFrontLeftSteerMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kFrontLeftSteerInverted,
                CANCoder(constants.kFrontLeftSteerEncoderId),
                constants.kFrontLeftAbsoluteEncoderOffset,
            )
            self.frontRightModule = REVSwerveModule(
                constants.kFrontRightModuleName,
                CANSparkMax(
                    constants.kFrontRightDriveMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kFrontRightDriveInverted,
                CANSparkMax(
                    constants.kFrontRightSteerMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kFrontRightSteerInverted,
                CANCoder(constants.kFrontRightSteerEncoderId),
                constants.kFrontRightAbsoluteEncoderOffset,
            )
            self.backLeftModule = REVSwerveModule(
                constants.kBackLeftModuleName,
                CANSparkMax(
                    constants.kBackLeftDriveMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kBackLeftDriveInverted,
                CANSparkMax(
                    constants.kBackLeftSteerMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kBackLeftSteerInverted,
                CANCoder(constants.kBackLeftSteerEncoderId),
                constants.kBackLeftAbsoluteEncoderOffset,
            )
            self.backRightModule = REVSwerveModule(
                constants.kBackRightModuleName,
                CANSparkMax(
                    constants.kBackRightDriveMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kBackRightDriveInverted,
                CANSparkMax(
                    constants.kBackRightSteerMotorId, CANSparkMax.MotorType.kBrushless
                ),
                constants.kBackRightSteerInverted,
                CANCoder(constants.kBackRightSteerEncoderId),
                constants.kBackRightAbsoluteEncoderOffset,
            )

        else:
            self.frontLeftModule = PWMSwerveModule(
                constants.kFrontLeftModuleName,
                PWMVictorSPX(constants.kFrontLeftDriveMotorSimPort),
                PWMVictorSPX(constants.kFrontLeftSteerMotorSimPort),
                Encoder(*constants.kFrontLeftDriveEncoderSimPorts),
                Encoder(*constants.kFrontLeftSteerEncoderSimPorts),
            )
            self.frontRightModule = PWMSwerveModule(
                constants.kFrontRightModuleName,
                PWMVictorSPX(constants.kFrontRightDriveMotorSimPort),
                PWMVictorSPX(constants.kFrontRightSteerMotorSimPort),
                Encoder(*constants.kFrontRightDriveEncoderSimPorts),
                Encoder(*constants.kFrontRightSteerEncoderSimPorts),
            )
            self.backLeftModule = PWMSwerveModule(
                constants.kBackLeftModuleName,
                PWMVictorSPX(constants.kBackLeftDriveMotorSimPort),
                PWMVictorSPX(constants.kBackLeftSteerMotorSimPort),
                Encoder(*constants.kBackLeftDriveEncoderSimPorts),
                Encoder(*constants.kBackLeftSteerEncoderSimPorts),
            )
            self.backRightModule = PWMSwerveModule(
                constants.kBackRightModuleName,
                PWMVictorSPX(constants.kBackRightDriveMotorSimPort),
                PWMVictorSPX(constants.kBackRightSteerMotorSimPort),
                Encoder(*constants.kBackRightDriveEncoderSimPorts),
                Encoder(*constants.kBackRightSteerEncoderSimPorts),
            )

        self.modules = (
            self.frontLeftModule,
            self.frontRightModule,
            self.backLeftModule,
            self.backRightModule,
        )

        self.kinematics = SwerveDrive4Kinematics(
            constants.kFrontLeftWheelPosition,
            constants.kFrontRightWheelPosition,
            constants.kBackLeftWheelPosition,
            constants.kBackRightWheelPosition,
        )

        # Create the gyro, a sensor which can indicate the heading of the robot relative
        # to a customizable position.
        self.gyro = AHRS.create_spi()

        # Create the an object for our odometry, which will utilize sensor data to
        # keep a record of our position on the field.
        self.odometry = SwerveDrive4Odometry(
            self.kinematics,
            self.getRotation(),
            (
                self.frontLeftModule.getPosition(),
                self.frontRightModule.getPosition(),
                self.backLeftModule.getPosition(),
                self.backRightModule.getPosition(),
            ),
            Pose2d(),
        )

        self.printTimer = Timer()
        # self.printTimer.start()

        self.returnPos = Pose2d(0, 0, 0)

    def resetSwerveModules(self):
        for module in self.modules:
            module.reset()
        self.gyro.reset()
        self.odometry.resetPosition(
            self.gyro.getRotation2d(),
            Pose2d(),
            self.frontLeftModule.getPosition(),
            self.frontRightModule.getPosition(),
            self.backLeftModule.getPosition(),
            self.backRightModule.getPosition(),
        )

    def setOdometryPosition(self, pose: Pose2d):
        self.gyro.setAngleAdjustment(pose.rotation().degrees())
        self.odometry.resetPosition(
            self.gyro.getRotation2d(),
            pose,
            self.frontLeftModule.getPosition(),
            self.frontRightModule.getPosition(),
            self.backLeftModule.getPosition(),
            self.backRightModule.getPosition(),
        )

    def resetGyro(self, pose: Pose2d):
        self.gyro.reset()
        self.gyro.setAngleAdjustment(pose.rotation().degrees())
        self.odometry.resetPosition(
            self.gyro.getRotation2d(),
            pose,
            self.frontLeftModule.getPosition(),
            self.frontRightModule.getPosition(),
            self.backLeftModule.getPosition(),
            self.backRightModule.getPosition(),
        )

    def getRotation(self) -> Rotation2d:
        return self.gyro.getRotation2d()

    def periodic(self):
        """
        Called periodically when it can be called. Updates the robot's
        odometry with sensor data.
        """
        self.odometry.update(
            self.getRotation(),
            self.frontLeftModule.getPosition(),
            self.frontRightModule.getPosition(),
            self.backLeftModule.getPosition(),
            self.backRightModule.getPosition(),
        )

        robotPose = self.odometry.getPose()

        robotPoseArray = [robotPose.X(), robotPose.Y(), robotPose.rotation().radians()]

        SmartDashboard.putNumberArray(constants.kRobotPoseArrayKeys, robotPoseArray)

        if self.printTimer.hasElapsed(constants.kPrintPeriod):
            rX = self.odometry.getPose().translation().X()
            rY = self.odometry.getPose().translation().Y()
            rAngle = int(self.odometry.getPose().rotation().degrees())

            flAngle = int(self.frontLeftModule.getSwerveAngle().degrees())
            frAngle = int(self.frontRightModule.getSwerveAngle().degrees())
            blAngle = int(self.backLeftModule.getSwerveAngle().degrees())
            brAngle = int(self.backRightModule.getSwerveAngle().degrees())

            flSpeed = self.frontLeftModule.getWheelLinearVelocity()
            frSpeed = self.frontRightModule.getWheelLinearVelocity()
            blSpeed = self.backLeftModule.getWheelLinearVelocity()
            brSpeed = self.backRightModule.getWheelLinearVelocity()

            print(
                "r: {:.1f}, {:.1f}, {}* fl: {}* {:.1f} fr: {}* {:.1f} bl: {}* {:.1f} br: {}* {:.1f}".format(
                    rX,
                    rY,
                    rAngle,
                    flAngle,
                    flSpeed,
                    frAngle,
                    frSpeed,
                    blAngle,
                    blSpeed,
                    brAngle,
                    brSpeed,
                )
            )

    def arcadeDriveWithFactors(
        self,
        forwardSpeedFactor: float,
        sidewaysSpeedFactor: float,
        rotationSpeedFactor: float,
        coordinateMode: CoordinateMode,
    ) -> None:
        """
        Drives the robot using arcade controls.

        :param forwardSpeedFactor: the commanded forward movement
        :param sidewaysSpeedFactor: the commanded sideways movement
        :param rotationSpeedFactor: the commanded rotation
        """
        # print(
        #     "inputs: x: {:.2f} y: {:.2f} *: {:.2f}".format(
        #         forwardSpeedFactor, sidewaysSpeedFactor, rotationSpeedFactor
        #     )
        # )
        chassisSpeeds = ChassisSpeeds(
            forwardSpeedFactor * constants.kMaxForwardLinearVelocity,
            sidewaysSpeedFactor * constants.kMaxSidewaysLinearVelocity,
            rotationSpeedFactor * constants.kMaxRotationAngularVelocity,
        )

        self.arcadeDriveWithSpeeds(chassisSpeeds, coordinateMode)

    def arcadeDriveWithSpeeds(
        self, chassisSpeeds: ChassisSpeeds, coordinateMode: CoordinateMode
    ) -> None:

        robotChassisSpeeds = None
        if coordinateMode is DriveSubsystem.CoordinateMode.RobotRelative:
            robotChassisSpeeds = chassisSpeeds
        elif coordinateMode is DriveSubsystem.CoordinateMode.FieldRelative:
            robotChassisSpeeds = ChassisSpeeds.fromFieldRelativeSpeeds(
                chassisSpeeds.vx,
                chassisSpeeds.vy,
                chassisSpeeds.omega,
                self.odometry.getPose().rotation(),
            )

        moduleStates = self.kinematics.toSwerveModuleStates(robotChassisSpeeds)
        (
            frontLeftState,
            frontRightState,
            backLeftState,
            backRightState,
        ) = SwerveDrive4Kinematics.desaturateWheelSpeeds(
            moduleStates, constants.kMaxWheelLinearVelocity
        )
        self.frontLeftModule.applyState(frontLeftState)
        self.frontRightModule.applyState(frontRightState)
        self.backLeftModule.applyState(backLeftState)
        self.backRightModule.applyState(backRightState)

    def rotatePoint(
        self, x: float, y: float, angle: float, ccw: bool
    ) -> Tuple[float, float]:
        invert = ccw * 2 - 1

        xNew = x * cos(angle) - y * sin(angle) * invert
        yNew = x * sin(angle) + y * cos(angle) * invert
        return xNew, yNew

    def shiftPoint(self, savedPose: Pose2d, currentPose: Pose2d) -> Pose2d:
        xDelta = savedPose.X() - currentPose.X()
        yDelta = savedPose.Y() - currentPose.Y()

        coords = self.rotatePoint(
            xDelta, yDelta, currentPose.rotation().radians() * -1, True
        )

        return Pose2d(coords[0], coords[1], 0)
