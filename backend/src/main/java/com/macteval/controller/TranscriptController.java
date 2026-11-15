package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.LessonTranscriptDTO;
import com.macteval.service.TranscriptService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/transcript")
@RequiredArgsConstructor
public class TranscriptController {

    private final TranscriptService transcriptService;

    @GetMapping("/{lessonSessionId}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<LessonTranscriptDTO>> getByLesson(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        return ApiResponse.ok(transcriptService.getByLesson(lessonSessionId, auth.getName()));
    }
}
