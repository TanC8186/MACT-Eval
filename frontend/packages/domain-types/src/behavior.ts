export type ObservableBehavior = 'in-seat' | 'leave-seat' | 'hand-raised' | 'standing' | 'upright-posture'

export type DataQuality = 'usable' | 'limited' | 'unavailable'

export interface BehaviorObservationWindow {
  lessonSessionId: string
  activityId?: string
  windowStart: string
  windowEnd: string
  visibleSeatCount: number
  totalConfiguredSeats: number
  coverage: number
  dataQuality: DataQuality
  observations: BehaviorObservation[]
}

export interface BehaviorObservation {
  type: ObservableBehavior
  proportion: number
  count: number
  duration?: number
}

export interface BehaviorTrend {
  lessonSessionId: string
  windows: BehaviorObservationWindow[]
  summary: {
    averageCoverage: number
    averageUprightPosture?: number
    handRaisedCount: number
    standingCount: number
    leaveSeatCount: number
  }
}
