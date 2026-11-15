package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.model.entity.SuggestionEntity;
import com.macteval.service.SuggestionService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/suggestions")
@RequiredArgsConstructor
public class SuggestionController {

    private final SuggestionService suggestionService;

    @PostMapping("/{id}/adopt")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<SuggestionEntity>> adopt(
            @PathVariable String id, Authentication auth) {
        return ApiResponse.ok(suggestionService.changeStatus(
                id, SuggestionService.Action.ADOPT, auth.getName(), isAdmin(auth)));
    }

    @PostMapping("/{id}/accept")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<SuggestionEntity>> accept(
            @PathVariable String id, Authentication auth) {
        return ApiResponse.ok(suggestionService.changeStatus(
                id, SuggestionService.Action.ACCEPT, auth.getName(), isAdmin(auth)));
    }

    @PostMapping("/{id}/ignore")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<SuggestionEntity>> ignore(
            @PathVariable String id, Authentication auth) {
        return ApiResponse.ok(suggestionService.changeStatus(
                id, SuggestionService.Action.IGNORE, auth.getName(), isAdmin(auth)));
    }

    private boolean isAdmin(Authentication auth) {
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
    }
}
